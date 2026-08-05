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

import zlib

import numpy as np
import pandas as pd

from . import aggregate, score
from .config import (
    ALL_COMPONENTS,
    BANDWIDTH_M,
    BANDWIDTH_SWEEP,
    BANDWIDTH_TRIALS,
    FIXED_VALUE_TRIALS,
    HOST_DISTANCE_TRIALS,
    HOST_MAX_DISTANCE_M,
    NOISE_IDW_MAX_DISTANCE_M,
    NOISE_IDW_SMOOTHING_M,
    PRESETS,
    PRIORITY_ALPHA,
    PRIORITY_BETA,
    SENSITIVITY_PERTURBATION,
    SENSITIVITY_TOP_K,
    SENSITIVITY_TRIALS,
)
from .schema import (
    ASSUMED_CAPACITY,
    ASSUMED_CAPACITY_DEFAULT,
    WELFARE_DEMAND_WEIGHT,
    WELFARE_DEMAND_WEIGHT_DEFAULT,
    ZONING_LOAD,
    ZONING_LOAD_DEFAULT,
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
# 4. 固定値の摂動（重み以外）
# ---------------------------------------------------------------------------
#
# 上の 3 つは **正規化済みの列に重みを掛け直しているだけ**で、
# レイヤーの重み 8 個しか触っていない。ところが結果を決めている固定値は
# 他にもある——仮定員 25 種・種別重み 30 種・帯域 4 値・用途地域の負荷 13 値・
# 合成指数 α/β・IDW の 2 値。「重みを変えても結論は変わりません」は
# **その 8 個についての主張でしかなかった**（docs/issues.md A3・D1）。
#
# ここはそれらを揺さぶる。集計まで遡って計算し直す必要があるので、
# aggregate.kernel_triplets と polygon_area_shares で重い部分を 1 度だけ作る。


def _perturb(values: dict, rng: np.random.Generator, p: float, hi: float | None = None) -> dict:
    """辞書の各値を独立に uniform(1-p, 1+p) 倍する。hi があれば上限で切る。"""
    out = {}
    for k, v in values.items():
        x = float(v) * float(rng.uniform(1 - p, 1 + p))
        out[k] = min(x, hi) if hi is not None else x
    return out


class FixedValueModel:
    """重み以外の固定値を差し替えて、優先度を計算し直せるようにする。

    `build_mesh_table` を毎回呼ぶと 1 試行 24 秒かかる（大半は根拠カード用の
    実数列と最寄り施設の探索で、優先度には効かない部分）。ここでは
    **優先度に効く 8 列だけ**を作り直す。

    さらに、帯域を変えない摂動ではカーネルの重みが変わらないことを使い、
    点→メッシュの三つ組を 1 度だけ作って使い回す。1 試行 20 ミリ秒になる。

    基準（何も差し替えない状態）が `build_mesh_table` の出力と一致することは
    selftest で検査する。**速い経路が本番と違う数字を出していたら、
    感度分析の結論そのものが無意味になる。**
    """

    # 需要側の 4 レイヤーは同じ形（点 × demand_value）で集計している。
    _POINT_LAYERS = {
        "welfare_capacity": "welfare",
        "sped_school": "schools",
        "station_flow": "stations",
        "clinic": "clinics",
    }

    def __init__(self, mesh_gdf, layers: dict):
        self.mesh = mesh_gdf
        self.layers = layers
        self.n = len(mesh_gdf)
        self.index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")

        # 優先度に効かない列（表示用の f_* や geometry）は持ち込まない。
        self.base_raw = pd.DataFrame(
            {c.key: mesh_gdf[c.key].to_numpy(dtype=float) for c in ALL_COMPONENTS},
            index=self.index,
        )

        self._kernels = {
            key: aggregate.kernel_triplets(mesh_gdf, layers[layer], BANDWIDTH_M[key])
            for key, layer in self._POINT_LAYERS.items()
        }
        self._zoning_classes, self._zoning_shares = aggregate.polygon_area_shares(
            mesh_gdf, layers["zoning"], "zoning_code"
        )
        # ポリゴンに覆われないメッシュは既定値。シェアの行和が 0 で見分ける。
        self._zoning_covered = self._zoning_shares.sum(axis=1) > 0

        self.base = self._compose(self.base_raw)
        self.base_top = {k: set(_top_codes(self.base, k)) for k in SENSITIVITY_TOP_K}
        self.base_rank = _ranks(self.base)
        self.n_assumed = int(self._assumed_mask().sum())

    # -- 生値列の作り直し ---------------------------------------------------

    def _assumed_mask(self) -> np.ndarray:
        w = self.layers["welfare"]
        if "capacity_assumed" not in w.columns:
            # 静かに「仮定員は 0 件」として通すと、A3 の群だけが
            # 何も揺すらないまま「100% 維持」と表示される。
            raise KeyError(
                "welfare レイヤーに capacity_assumed 列が無い。"
                "python -m etl.fetch --normalize wamnet ... --replace で作り直すこと"
            )
        return w["capacity_assumed"].fillna(False).to_numpy(dtype=bool)

    def _welfare_demand(
        self,
        weights: dict[str, float] | None,
        capacities: dict[str, float] | None,
        drop_assumed: bool = False,
        ignore_capacity: bool = False,
    ) -> np.ndarray:
        """事業所 1 件ごとの需要寄与（定員 × 種別重み）を作り直す。"""
        w = self.layers["welfare"]
        kinds = w["kind"].astype(str)
        cap = w["capacity"].to_numpy(dtype=float).copy()
        assumed = self._assumed_mask()

        if capacities is not None:
            replaced = kinds.map(
                lambda k: capacities.get(k, ASSUMED_CAPACITY_DEFAULT)
            ).to_numpy(dtype=float)
            cap = np.where(assumed, replaced, cap)
        if ignore_capacity:
            cap = np.ones_like(cap)

        if weights is None:
            wt = w["weight"].to_numpy(dtype=float)
        else:
            wt = kinds.map(
                lambda k: weights.get(k, WELFARE_DEMAND_WEIGHT_DEFAULT)
            ).to_numpy(dtype=float)

        value = cap * wt
        if drop_assumed:
            value = np.where(assumed, 0.0, value)
        return value

    def raw_columns(
        self,
        *,
        welfare_weights: dict[str, float] | None = None,
        assumed_capacities: dict[str, float] | None = None,
        drop_assumed: bool = False,
        ignore_capacity: bool = False,
        bandwidths: dict[str, float] | None = None,
        zoning_loads: dict[int, float] | None = None,
        zoning_default: float | None = None,
        idw: dict[str, float] | None = None,
    ) -> pd.DataFrame:
        """差し替えた固定値で、優先度に効く生値列を作り直す。"""
        raw = self.base_raw.copy()

        touch_welfare = (
            welfare_weights is not None
            or assumed_capacities is not None
            or drop_assumed
            or ignore_capacity
        )
        bw = dict(BANDWIDTH_M)
        if bandwidths:
            bw.update(bandwidths)

        for key, layer in self._POINT_LAYERS.items():
            changed_bw = bw[key] != BANDWIDTH_M[key]
            if key == "welfare_capacity" and (touch_welfare or changed_bw):
                values = self._welfare_demand(
                    welfare_weights, assumed_capacities, drop_assumed, ignore_capacity
                )
            elif changed_bw:
                values = self.layers[layer]["demand_value"].fillna(0).to_numpy(float)
            else:
                continue
            triplets = (
                aggregate.kernel_triplets(self.mesh, self.layers[layer], bw[key])
                if changed_bw
                else self._kernels[key]
            )
            raw[key] = aggregate.apply_kernel(triplets, values, self.n)

        if zoning_loads is not None or zoning_default is not None:
            loads = dict(ZONING_LOAD)
            if zoning_loads:
                loads.update(zoning_loads)
            default = ZONING_LOAD_DEFAULT if zoning_default is None else zoning_default
            vec = np.array(
                [loads.get(int(c), default) for c in self._zoning_classes], dtype=float
            )
            z = self._zoning_shares @ vec
            raw["zoning"] = np.where(self._zoning_covered, z, default)

        if idw is not None:
            noise = aggregate.idw_to_mesh(
                self.mesh, self.layers["noise"], "laeq_db", **idw
            )
            raw["noise"] = noise.fillna(noise.median()).to_numpy()

        return raw

    # -- 合成 ---------------------------------------------------------------

    def _compose(
        self, raw: pd.DataFrame, alpha: float = PRIORITY_ALPHA, beta: float = PRIORITY_BETA
    ) -> pd.DataFrame:
        """build.py と同じ順序で正規化 → 配信精度へ丸め → 合成する。"""
        normalized = score.normalize_components(raw)
        for c in ALL_COMPONENTS:
            normalized[f"n_{c.key}"] = score.publish_round(normalized[f"n_{c.key}"])
        out = score.compose(normalized, score.default_weights(), alpha, beta)
        out["mesh_code"] = self.index
        return out

    def evaluate(self, alpha: float = PRIORITY_ALPHA, beta: float = PRIORITY_BETA, **kw) -> dict:
        """固定値を差し替えて、基準の上位集合とどれだけ重なるかを返す。"""
        trial = self._compose(self.raw_columns(**kw), alpha, beta)
        r = _ranks(trial)
        watch = _top_codes(self.base, max(SENSITIVITY_TOP_K))
        return {
            "overlap": {
                str(k): len(self.base_top[k] & set(_top_codes(trial, k))) / k
                for k in SENSITIVITY_TOP_K
            },
            "rank_shift_median": float(
                np.median([abs(r[c] - self.base_rank[c]) for c in watch])
            ),
        }


def _summarize(samples: list[dict]) -> dict:
    """試行の集まりを、既存の random_perturbation と同じ形にまとめる。"""
    keys = samples[0]["overlap"].keys()
    return {
        "overlap_mean": {
            k: round(float(np.mean([s["overlap"][k] for s in samples])), 4) for k in keys
        },
        "overlap_min": {
            k: round(float(np.min([s["overlap"][k] for s in samples])), 4) for k in keys
        },
        "rank_shift_median": round(
            float(np.median([s["rank_shift_median"] for s in samples])), 1
        ),
    }


def fixed_value_perturbation(
    model: FixedValueModel,
    perturbation: float = SENSITIVITY_PERTURBATION,
    trials: int = FIXED_VALUE_TRIALS,
    seed: int = 43,
) -> list[dict]:
    """固定値の群ごとに、±p% ランダムに揺さぶって上位集合の安定性を測る。

    群に分けるのは「どの仮定が効いているか」を切り分けるため。
    最後の `all` は全部を同時に動かす——**個別に効かなくても
    重なると動くことがある**ので、合計を出さずに済ませない。
    """
    rng = np.random.default_rng(seed)
    p = perturbation

    groups: list[tuple[str, str, int, callable]] = [
        (
            "welfare_weight",
            "障害福祉サービスの種別重み",
            len(WELFARE_DEMAND_WEIGHT),
            lambda: {"welfare_weights": _perturb(WELFARE_DEMAND_WEIGHT, rng, p)},
        ),
        (
            "assumed_capacity",
            "定員の無い種別に当てている仮定員",
            len(ASSUMED_CAPACITY),
            lambda: {"assumed_capacities": _perturb(ASSUMED_CAPACITY, rng, p)},
        ),
        (
            "bandwidth",
            # 値は BANDWIDTH_M から組む。手で書くと、帯域を直したときに
            # **表示だけが古い数字のまま残る**（実際 1200m を 800m にしたとき、
            # ここのラベルだけが「800m / 1200m / 600m」と言い続けた）。
            "徒歩圏の帯域（"
            + " / ".join(f"{v:.0f}m" for v in sorted(set(BANDWIDTH_M.values()), reverse=True))
            + "）",
            len(BANDWIDTH_M),
            lambda: {"bandwidths": _perturb(BANDWIDTH_M, rng, p)},
        ),
        (
            "zoning_load",
            "用途地域 → 負荷の写像（13 値）",
            len(ZONING_LOAD),
            # 負荷は 0〜1 の絶対尺度なので、上へは 1.0 で切る。
            lambda: {"zoning_loads": _perturb(ZONING_LOAD, rng, p, hi=1.0)},
        ),
        (
            "idw",
            f"騒音の内挿（平滑化 {NOISE_IDW_SMOOTHING_M:.0f}m・"
            f"打ち切り {NOISE_IDW_MAX_DISTANCE_M:.0f}m）",
            2,
            lambda: {
                "idw": {
                    "smoothing_m": NOISE_IDW_SMOOTHING_M * rng.uniform(1 - p, 1 + p),
                    "max_distance_m": NOISE_IDW_MAX_DISTANCE_M
                    * rng.uniform(1 - p, 1 + p),
                }
            },
        ),
    ]

    out = []
    for gid, label, n_const, draw in groups:
        samples = [model.evaluate(**draw()) for _ in range(trials)]
        out.append(
            {"id": gid, "label": label, "constants": n_const, **_summarize(samples)}
        )

    # α/β は数が 2 つしかなく、掛け算モデルの形そのものを決める。
    ab = [
        model.evaluate(
            alpha=PRIORITY_ALPHA * rng.uniform(1 - p, 1 + p),
            beta=PRIORITY_BETA * rng.uniform(1 - p, 1 + p),
        )
        for _ in range(trials)
    ]
    out.append(
        {
            "id": "priority_exponent",
            "label": "合成指数 α・β",
            "constants": 2,
            **_summarize(ab),
        }
    )

    # 全部同時。ここが「重み以外の固定値を全部揺すっても」の答えになる。
    both = [
        model.evaluate(
            welfare_weights=_perturb(WELFARE_DEMAND_WEIGHT, rng, p),
            assumed_capacities=_perturb(ASSUMED_CAPACITY, rng, p),
            bandwidths=_perturb(BANDWIDTH_M, rng, p),
            zoning_loads=_perturb(ZONING_LOAD, rng, p, hi=1.0),
            idw={
                "smoothing_m": NOISE_IDW_SMOOTHING_M * rng.uniform(1 - p, 1 + p),
                "max_distance_m": NOISE_IDW_MAX_DISTANCE_M * rng.uniform(1 - p, 1 + p),
            },
            alpha=PRIORITY_ALPHA * rng.uniform(1 - p, 1 + p),
            beta=PRIORITY_BETA * rng.uniform(1 - p, 1 + p),
        )
        for _ in range(trials)
    ]
    out.append(
        {
            "id": "all",
            "label": "上の全部を同時に",
            "constants": sum(g[2] for g in groups) + 2,
            **_summarize(both),
        }
    )
    return out


def bandwidth_profile(
    model: FixedValueModel, factors: tuple[float, ...] = BANDWIDTH_SWEEP
) -> list[dict]:
    """帯域を 1 層ずつ、範囲を掃いて動かしたときの上位集合の重なり。

    **±30% を 4 層まとめて揺さぶった数字（平均 76%・最悪 30%）は、
    どの帯域が効いているかを答えない。** 4 つのうち 1 つが原因なのか、
    4 つとも同じくらい効いているのかで、次にやることが変わる。

    range を掃くのは、乱数の摂動より答えが具体的になるから。
    「±30% で 76%」は「値が少し違ったら」しか言えないが、
    「600〜1000m の範囲なら上位 10 件は 90% 維持、400m で崩れる」なら
    **その帯域をどこまで信用してよいかが読める**。

    帯域には `AbsoluteScale` の `basis` に相当する外部の根拠が無い
    （「徒歩10分 ≒ 800m」という目安から置いた値）。根拠を作れない以上、
    せめて**どの範囲まで結論が変わらないか**は出しておく。
    """
    out = []
    for key, layer in FixedValueModel._POINT_LAYERS.items():
        base_m = BANDWIDTH_M[key]
        label = next((c.label for c in ALL_COMPONENTS if c.key == key), key)
        points = []
        for f in factors:
            m = base_m * f
            r = model.evaluate(bandwidths={key: m}) if f != 1.0 else {
                "overlap": {str(k): 1.0 for k in SENSITIVITY_TOP_K},
                "rank_shift_median": 0.0,
            }
            points.append(
                {
                    "bandwidth_m": round(m, 1),
                    "factor": f,
                    "overlap": {k: round(v, 3) for k, v in r["overlap"].items()},
                    "rank_shift_median": round(r["rank_shift_median"], 1),
                }
            )
        # その層だけを ±30% 揺さぶった値。群（4 層同時）の 76% を分解する。
        #
        # **種は `hash(key)` で作ってはいけない。** Python の文字列 hash は
        # プロセスごとに乱数化されるので（PYTHONHASHSEED）、同じ入力・同じ
        # コードで走らせても毎回違う値が出る。他の摂動は固定種（42/43）で
        # 再現するのに、ここだけが再現しなかった——**再現できない数字を
        # 文書に引用していた**ことになる。crc32 は版にも環境にも依存しない。
        rng = np.random.default_rng(zlib.crc32(key.encode("utf-8")))
        samples = [
            model.evaluate(
                bandwidths={
                    key: base_m * float(rng.uniform(1 - SENSITIVITY_PERTURBATION,
                                                    1 + SENSITIVITY_PERTURBATION))
                }
            )
            for _ in range(BANDWIDTH_TRIALS)
        ]
        out.append(
            {
                "key": key,
                "label": label,
                "layer": layer,
                "default_m": base_m,
                "alone": _summarize(samples),
                "sweep": points,
            }
        )
    # 単独で最も効く層から並べる。
    out.sort(key=lambda d: d["alone"]["overlap_mean"]["10"])
    return out


def assumption_scenarios(model: FixedValueModel) -> list[dict]:
    """仮定そのものを外したときに上位がどうなるか。

    ±30% の摂動は「値が少し違ったら」しか答えない。**その仮定を
    そもそも置かなかったら**という問いには、値を消す側で答える。
    A3 に対して一番強い言い方ができるのはここ——
    「仮定員の行を需要から全部落としても上位は◯% 残る」。
    """
    scenarios = [
        (
            "drop_assumed_capacity",
            f"仮定員の {model.n_assumed:,} 件を需要から全部落とす",
            {"drop_assumed": True},
        ),
        (
            "ignore_capacity",
            "定員を使わず事業所の件数だけで需要を作る",
            {"ignore_capacity": True},
        ),
        (
            "flat_welfare_weight",
            "サービス種別を区別しない（全種別を既定重み 0.50 に）",
            {
                "welfare_weights": {
                    k: WELFARE_DEMAND_WEIGHT_DEFAULT for k in WELFARE_DEMAND_WEIGHT
                }
            },
        ),
        (
            "flat_zoning_load",
            "用途地域を区別しない（13 値を全て既定 0.30 に）",
            {
                "zoning_loads": {k: ZONING_LOAD_DEFAULT for k in ZONING_LOAD},
                "zoning_default": ZONING_LOAD_DEFAULT,
            },
        ),
    ]
    out = []
    for sid, label, kw in scenarios:
        r = model.evaluate(**kw)
        out.append(
            {
                "id": sid,
                "label": label,
                "overlap": {k: round(v, 3) for k, v in r["overlap"].items()},
                "rank_shift_median": round(r["rank_shift_median"], 1),
            }
        )
    return out


def host_distance_report(scored: pd.DataFrame, mesh_gdf, hosts) -> list[dict]:
    """徒歩圏 700m を動かしたときの到達不可件数。

    **この値は優先度を動かさない**（供給側はスコアの構成要素ではない）。
    動くのは「既存施設では到達不可」という出力の方で、
    そこは 700m という 1 つの数字にそのまま乗っている。

    区をまたいで並べてよい方の指標（区内で優先度が中位以上）も併せて出す。
    """
    from . import hosts as hostlib

    out = []
    for radius in HOST_DISTANCE_TRIALS:
        n_host = aggregate.count_within(mesh_gdf, hosts, radius).to_numpy()
        trial = scored.copy()
        trial["f_host_n"] = n_host
        reach = hostlib.reach_report(trial)
        unreachable = int(reach["到達不可"].sum())
        out.append(
            {
                "radius_m": radius,
                "unreachable": unreachable,
                "unreachable_ratio": round(unreachable / len(trial), 4),
                "mid_or_above": int(reach["中位以上"].sum()),
            }
        )
    return out


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def run(df: pd.DataFrame, model: FixedValueModel | None = None) -> dict:
    """感度分析をまとめて実行する。df は normalize_components 済みのもの。

    model を渡すと、重み以外の固定値の摂動も走る。渡せるのは
    build.py だけ（メッシュ形状と入力レイヤーが要る）。
    """
    out = {
        "random_perturbation": random_perturbation(df),
        "leave_one_out": leave_one_out(df),
        "preset_agreement": preset_agreement(df),
    }
    if model is not None:
        out["fixed_values"] = {
            "perturbation": SENSITIVITY_PERTURBATION,
            "trials": FIXED_VALUE_TRIALS,
            "assumed_capacity_rows": model.n_assumed,
            "groups": fixed_value_perturbation(model),
            "scenarios": assumption_scenarios(model),
            "bandwidth_profile": bandwidth_profile(model),
        }
    return out


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

    fv = result.get("fixed_values")
    if fv:
        lines += [
            "",
            "[感度] 重み以外の固定値を ±{:.0%} 揺さぶる（{}回試行 / 上位10件の重なり）".format(
                fv["perturbation"], fv["trials"]
            ),
        ]
        for g in fv["groups"]:
            lines.append(
                f"         {g['label'][:28]:<30} {g['constants']:>3}個  "
                f"平均 {g['overlap_mean']['10']:.0%}  最悪 {g['overlap_min']['10']:.0%}  "
                f"順位変動 中央値 {g['rank_shift_median']:.0f}位"
            )
        lines += [
            "",
            "[感度] 仮定そのものを外したとき（上位10 / 20 / 50件の重なり）",
        ]
        for s in fv["scenarios"]:
            o = s["overlap"]
            lines.append(
                f"         {s['label'][:34]:<36} "
                f"{o['10']:.0%} / {o['20']:.0%} / {o['50']:.0%}"
            )

    hd = result.get("host_distance")
    if hd:
        lines += [
            "",
            "[感度] 徒歩圏 700m を動かしたときの到達不可（優先度は動かない）",
        ]
        for r in hd:
            mark = " ←既定" if r["radius_m"] == HOST_MAX_DISTANCE_M else ""
            lines.append(
                f"         {r['radius_m']:>6.0f}m  到達不可 {r['unreachable']:>5,}件 "
                f"({r['unreachable_ratio']:.1%})  うち区内で中位以上 "
                f"{r['mid_or_above']:>4,}件{mark}"
            )
    return "\n".join(lines)
