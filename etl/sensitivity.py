"""
感度分析 — モデルがどれだけ重みに依存しているかを測る。

「その重み付けは恣意的では?」への回答をスライダーだけに委ねると、
審査員が実際に動かしてくれるかどうかに賭けることになる。
そこで **動かした結果がどうなるかを、あらかじめ数値で出しておく**。

3 通りの測り方を用意する。答えたい問いがそれぞれ違う。

    1. ランダム摂動  … 「重みを ±30% 適当にずらしたら順位は変わるか」
    2. Leave-one-out … 「どのレイヤーを外すと結果が壊れるか」
    3. プリセット比較 … 「立場が変わっても共通して上位に来る場所はどこか」

3 は特に提言に直結する。聴覚過敏を重視する人と、通所需要を重視する人と、
鉄道事業者、全員の上位に共通して入る区画があるなら、
それは重みの選び方に関係なく整備すべき場所だということになる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import score
from .config import (
    ALL_COMPONENTS,
    PRESETS,
    PRIORITY_ALPHA,
    PRIORITY_BETA,
    SENSITIVITY_PERTURBATION,
    SENSITIVITY_TOP_K,
    SENSITIVITY_TRIALS,
)


def _top_codes(df: pd.DataFrame, k: int) -> list[str]:
    """優先度上位 k 件のメッシュコード（降順）。"""
    return df.nlargest(k, "priority")["mesh_code"].tolist()


def _ranks(df: pd.DataFrame) -> pd.Series:
    """メッシュコード → 順位（1 が最上位）。"""
    order = df.sort_values("priority", ascending=False)["mesh_code"].tolist()
    return pd.Series({code: i + 1 for i, code in enumerate(order)})


def _compose(df: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    return score.compose(df, weights, PRIORITY_ALPHA, PRIORITY_BETA)


def _resolve(preset_weights: dict[str, float]) -> dict[str, float]:
    """プリセットの部分指定を、全レイヤー分の重みに展開する。"""
    return {c.key: float(preset_weights.get(c.key, c.weight)) for c in ALL_COMPONENTS}


# ---------------------------------------------------------------------------
# 1. ランダム摂動
# ---------------------------------------------------------------------------


def random_perturbation(
    df: pd.DataFrame,
    perturbation: float = SENSITIVITY_PERTURBATION,
    trials: int = SENSITIVITY_TRIALS,
    seed: int = 42,
) -> dict:
    """全レイヤーの重みを同時にランダムに揺さぶり、上位集合の安定性を測る。

    各重みを独立に uniform(1-p, 1+p) 倍する。
    「重み付けを少し変えたら結論が変わるのか」に直接答える。
    """
    rng = np.random.default_rng(seed)
    base = _compose(df, score.default_weights())
    base_top = {k: set(_top_codes(base, k)) for k in SENSITIVITY_TOP_K}
    base_rank = _ranks(base)
    watch = _top_codes(base, max(SENSITIVITY_TOP_K))

    overlaps: dict[int, list[float]] = {k: [] for k in SENSITIVITY_TOP_K}
    shifts: list[float] = []

    for _ in range(trials):
        w = {
            c.key: c.weight * float(rng.uniform(1 - perturbation, 1 + perturbation))
            for c in ALL_COMPONENTS
        }
        trial = _compose(df, w)
        for k in SENSITIVITY_TOP_K:
            overlaps[k].append(len(base_top[k] & set(_top_codes(trial, k))) / k)
        r = _ranks(trial)
        shifts.extend(abs(r[c] - base_rank[c]) for c in watch)

    shifts_arr = np.array(shifts, dtype=float)
    return {
        "perturbation": perturbation,
        "trials": trials,
        "overlap_mean": {str(k): round(float(np.mean(v)), 4) for k, v in overlaps.items()},
        "overlap_min": {str(k): round(float(np.min(v)), 4) for k, v in overlaps.items()},
        "rank_shift_median": round(float(np.median(shifts_arr)), 1),
        "rank_shift_p90": round(float(np.percentile(shifts_arr, 90)), 1),
    }


# ---------------------------------------------------------------------------
# 2. Leave-one-out
# ---------------------------------------------------------------------------


def leave_one_out(df: pd.DataFrame, k: int = 10) -> list[dict]:
    """各レイヤーの重みを 0 にして、上位集合がどれだけ入れ替わるかを測る。

    重なりが小さいほど、そのレイヤー 1 本に結論が依存しているということ。
    「用途地域を感覚負荷の予測器に使う」という主張が実際に効いているのかも、
    ここで正直に検算できる。効いていなければ主張を弱めるべきである。
    """
    base = _compose(df, score.default_weights())
    base_top = set(_top_codes(base, k))

    out = []
    for c in ALL_COMPONENTS:
        w = score.default_weights()
        w[c.key] = 0.0
        trial = _compose(df, w)
        overlap = len(base_top & set(_top_codes(trial, k))) / k
        out.append(
            {
                "key": c.key,
                "label": c.label,
                "side": c.side,
                "weight": c.weight,
                f"overlap_top{k}": round(overlap, 3),
            }
        )
    # 重なりが小さい＝影響が大きい順に並べる。
    out.sort(key=lambda d: d[f"overlap_top{k}"])
    return out


# ---------------------------------------------------------------------------
# 3. プリセット比較
# ---------------------------------------------------------------------------


def preset_agreement(df: pd.DataFrame, k: int = 20) -> dict:
    """全プリセットの上位 k 件を突き合わせ、共通して選ばれる区画を出す。

    立場を変えても共通して上位に来る場所は、
    重みの選び方に関係なく整備すべき場所だと言える。提言の核になる。
    """
    tops: dict[str, list[str]] = {}
    for preset in PRESETS:
        result = _compose(df, _resolve(preset["weights"]))
        tops[preset["id"]] = _top_codes(result, k)

    sets = [set(v) for v in tops.values()]
    common = set.intersection(*sets) if sets else set()

    # 共通区画を既定重みでの順位順に並べる。
    base = _compose(df, score.default_weights())
    base_rank = _ranks(base)
    common_sorted = sorted(common, key=lambda c: base_rank[c])

    pairwise = {}
    ids = list(tops)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pairwise[f"{a}×{b}"] = round(
                len(set(tops[a]) & set(tops[b])) / k, 3
            )

    return {
        "top_k": k,
        "preset_ids": ids,
        "common_count": len(common),
        "common_ratio": round(len(common) / k, 3),
        "common_meshes": common_sorted,
        "pairwise_overlap": pairwise,
    }


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def run(df: pd.DataFrame) -> dict:
    """3 種の分析をまとめて実行する。df は normalize_components 済みのもの。"""
    return {
        "random_perturbation": random_perturbation(df),
        "leave_one_out": leave_one_out(df),
        "preset_agreement": preset_agreement(df),
    }


def format_report(result: dict) -> str:
    """人が読む形に整える。ビルド時の標準出力用。"""
    rp = result["random_perturbation"]
    pa = result["preset_agreement"]
    lines = [
        "",
        "[感度] 重みを ±{:.0%} ランダムに揺さぶる（{}回試行）".format(
            rp["perturbation"], rp["trials"]
        ),
        "       上位に残り続けた割合:",
    ]
    for k, v in rp["overlap_mean"].items():
        worst = rp["overlap_min"][k]
        lines.append(
            f"         上位{k:>3}件  平均 {v:.1%}  最悪 {worst:.1%}"
        )
    lines.append(
        f"       順位の変動: 中央値 {rp['rank_shift_median']:.0f}位 / "
        f"90%点 {rp['rank_shift_p90']:.0f}位"
    )

    lines += [
        "",
        "[感度] レイヤーを1つ外したときの上位10件の重なり（小さいほど依存が大きい）",
    ]
    for row in result["leave_one_out"]:
        lines.append(
            f"         {row['label'][:24]:<26} {row['overlap_top10']:.0%}"
        )

    lines += [
        "",
        f"[感度] {len(pa['preset_ids'])}つのプリセット全てで上位{pa['top_k']}件に入った区画: "
        f"{pa['common_count']}件 ({pa['common_ratio']:.0%})",
    ]
    if pa["common_meshes"]:
        lines.append("         " + " ".join(pa["common_meshes"][:8]))
    return "\n".join(lines)
