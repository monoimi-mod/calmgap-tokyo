"""
設置優先度スコアの算出。

    優先度 = 需要スコア × 負荷スコア

足し算ではなく掛け算にする。人がいて「かつ」過負荷、
の両方が揃った場所だけを立てるため。静かな住宅地に当事者が住んでいても
公共の退避場所は要らないし、うるさくても誰も通らない場所にも要らない。
足し算だと、どちらか一方が極端に高いだけの場所が上位に紛れ込む。

重要な設計方針:
    最終スコアではなく「正規化済みの各構成要素」をフロントへ配信し、
    重みの掛け合わせはブラウザ側で行う。こうすることで
    「なぜその重みなのか」という当然の批判を、
    審査員自身が動かせるスライダーへ転化できる。
    本モジュールの compose() と web/src/score.ts は同一の式でなければならない。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import (
    ALL_COMPONENTS,
    DEMAND_COMPONENTS,
    LOAD_COMPONENTS,
    PRIORITY_ALPHA,
    PRIORITY_BETA,
    PUBLISH_DECIMALS,
    Component,
)


# ---------------------------------------------------------------------------
# 配信精度への丸め
# ---------------------------------------------------------------------------


def publish_round(values, decimals: int = PUBLISH_DECIMALS):
    """配信精度へ丸める。**JavaScript の Math.round と同じ規則で**丸める。

    Python の組み込み round と numpy は「ちょうど半分」を偶数側へ丸める
    （0.03125 → 0.0312）。JavaScript の Math.round は大きい側へ丸める
    （0.03125 → 0.0313）。同じ数式を 2 言語で実装しているこのプロジェクトでは、
    この違いがそのまま Python↔TypeScript の不一致になる。

    模擬データではパーセンタイル順位が 1/32 のような綺麗な分数になるため
    ちょうど半分の値が実際に現れ、`python -m etl.build` の直後に
    parity_check が落ちていた（実データでは偶然この値に当たらず通っていた）。

    スコアは常に 0 以上なので floor(x + 0.5) で JS と同じ結果になる。
    """
    scale = 10.0**decimals
    if isinstance(values, pd.Series):
        return np.floor(values.to_numpy(dtype=float) * scale + 0.5) / scale
    return float(np.floor(float(values) * scale + 0.5) / scale)


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------


def percentile_normalize(
    values: pd.Series, zero_is_absence: bool = True
) -> pd.Series:
    """パーセンタイル順位で 0〜1 に正規化する。

    生の値をそのまま重み付き加算できない理由は単位が違うから。
    定員（人）と騒音（dB）と被覆率（比）を足すことに意味を持たせるには、
    いったん順位という共通の物差しへ載せ替える必要がある。
    絶対値ではなく順位を使うのは、外れ値（巨大ターミナル 1 駅）に
    スコア全体が支配されるのを防ぐため。

    zero_is_absence=True:
        0 は「存在しない」。厳密に 0 を返す。
        正の値だけを順位付けし (0, 1] に配分する。
    zero_is_absence=False:
        全体を順位付けし [0, 1] に配分する。
    """
    v = pd.to_numeric(values, errors="coerce").fillna(0.0)
    out = pd.Series(np.zeros(len(v)), index=v.index, dtype=float)

    if zero_is_absence:
        pos = v > 0
        n = int(pos.sum())
        if n == 0:
            return out
        if n == 1:
            out[pos] = 1.0
            return out
        # 順位 / 件数 → (0, 1]。最小の正値でも 0（＝不在）より確実に大きい。
        out[pos] = v[pos].rank(method="average") / n
        return out

    n = len(v)
    if n <= 1 or v.nunique() <= 1:
        return out
    return (v.rank(method="average") - 1.0) / (n - 1.0)


def normalize_components(
    mesh_df: pd.DataFrame, components: tuple[Component, ...] = ALL_COMPONENTS
) -> pd.DataFrame:
    """各構成要素の生値列を正規化し `n_<key>` 列として追加する。"""
    out = mesh_df.copy()
    for c in components:
        raw = out[c.key] if c.key in out.columns else pd.Series(0.0, index=out.index)
        out[f"n_{c.key}"] = percentile_normalize(raw, c.zero_is_absence)
    return out


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------


def _weighted_sum(
    df: pd.DataFrame, components: tuple[Component, ...], weights: dict[str, float]
) -> tuple[pd.Series, float]:
    """重み付き和と、重みの絶対値合計を返す。

    絶対値合計は「その側を評価しているか」の判定に使う（compose 参照）。
    """
    total = pd.Series(np.zeros(len(df)), index=df.index, dtype=float)
    total_abs_weight = 0.0
    for c in components:
        w = float(weights.get(c.key, c.weight))
        if w == 0.0:
            continue
        total_abs_weight += abs(w)
        total = total + w * c.sign * df[f"n_{c.key}"].astype(float)
    return total, total_abs_weight


def compose(
    df: pd.DataFrame,
    weights: dict[str, float] | None = None,
    alpha: float = PRIORITY_ALPHA,
    beta: float = PRIORITY_BETA,
) -> pd.DataFrame:
    """正規化済み列から demand / load / priority を計算する。

    web/src/score.ts と完全に同じ手順であること:
      1. 需要側・負荷側それぞれで重み付き和を取る（減点レイヤーは sign=-1）
      2. 和をもう一度パーセンタイル正規化して 0〜1 に戻す
         （重みの合計が変わってもスケールが一定になり、スライダー操作で
           色が飛ばない。相対比較のツールなので絶対値には意味を持たせない）
      3. priority = demand^alpha * load^beta
      4. 表示用に priority もパーセンタイル化する
    """
    weights = weights or {}
    out = df.copy()

    demand_raw, demand_w = _weighted_sum(out, DEMAND_COMPONENTS, weights)
    load_raw, load_w = _weighted_sum(out, LOAD_COMPONENTS, weights)

    # 片側の重みを全て 0 にした場合、その側は「評価しない」＝中立の 1.0 として
    # 掛け算から外す。0 のままだと全メッシュの優先度が 0 になり地図が消える。
    # web/src/score.ts の compose() と同じ扱い。
    if demand_w == 0.0:
        out["demand"] = 1.0
    else:
        # 需要は「不在なら 0」を維持する。掛け算モデルの肝。
        out["demand"] = percentile_normalize(demand_raw, zero_is_absence=True)

    if load_w == 0.0:
        out["load"] = 1.0
    else:
        # 負荷は減点レイヤーで負値になり得る連続量。全体を順位付けする。
        out["load"] = percentile_normalize(load_raw, zero_is_absence=False)

    # 区単位の手帳所持率など、面的でない補正係数（既定 1.0）。
    coef = (
        out["ward_coefficient"].astype(float)
        if "ward_coefficient" in out.columns
        else pd.Series(1.0, index=out.index)
    )
    out["demand"] = (out["demand"] * coef).clip(0.0, None)

    out["priority_raw"] = np.power(out["demand"].clip(0, None), alpha) * np.power(
        out["load"].clip(0, None), beta
    )
    out["priority"] = percentile_normalize(out["priority_raw"], zero_is_absence=True)
    return out


def default_weights() -> dict[str, float]:
    """既定重み。フロントのスライダー初期値と共有する。"""
    return {c.key: c.weight for c in ALL_COMPONENTS}


# ---------------------------------------------------------------------------
# 妥当性チェック
# ---------------------------------------------------------------------------


def sanity_report(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """上位メッシュの内訳を表で返す。

    ハンドオフ 7. のサニティチェック用。上位が大ターミナル周辺など
    「当事者や報道が知る実際にキツい場所」と一致するかを目視するための出力。
    """
    cols = ["mesh_code", "priority", "demand", "load"] + [
        f"n_{c.key}" for c in ALL_COMPONENTS
    ]
    cols = [c for c in cols if c in df.columns]
    return df.nlargest(top_n, "priority")[cols].round(3)


def correlation_report(df: pd.DataFrame) -> pd.DataFrame:
    """構成要素間の相関。1 つの現象を二重計上していないかの点検用。

    例えば駅乗降規模と昼間人口が 0.9 を超えるなら、
    実質的に同じ変数を 2 回足していることになり、重みの再検討が要る。
    """
    cols = [f"n_{c.key}" for c in ALL_COMPONENTS if f"n_{c.key}" in df.columns]
    return df[cols].corr().round(3)
