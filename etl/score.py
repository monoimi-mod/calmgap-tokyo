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
    AbsoluteScale,
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

    ⚠️ **順位化は分布の広さを捨てる。** 対象地域内で実質的に一様な層でも
    必ず 0〜1 いっぱいに引き伸ばされるため、識別力の無い層ほど差が誇張され、
    測定の偏りがあればそれごと増幅される。外部の基準で 0 と 1 を決められる層は
    absolute_normalize を使うこと（config.AbsoluteScale）。
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


def absolute_normalize(values: pd.Series, scale: AbsoluteScale) -> pd.Series:
    """外部の基準に固定した尺度で 0〜1 に正規化する。

    lo 以下を 0、hi 以上を 1 とする線形写像。パーセンタイル正規化と違い、
    **他のメッシュの値に依存しない**。あるメッシュが 70dB なら、
    周りが静かでもうるさくても 0.75 になる。

    これが効くのは、その層が対象地域内で実際には狭い範囲に収まっている場合。
    順位化だとその狭さが見えなくなるが、絶対尺度なら「差が小さい」ことが
    そのままスコアの差の小ささとして現れる（docs/issues.md A1）。
    """
    v = pd.to_numeric(values, errors="coerce").fillna(float(scale.lo))
    span = float(scale.hi) - float(scale.lo)
    if span <= 0:
        raise ValueError(f"AbsoluteScale の lo < hi が成り立たない: {scale}")
    return ((v - float(scale.lo)) / span).clip(0.0, 1.0)


def normalize_components(
    mesh_df: pd.DataFrame, components: tuple[Component, ...] = ALL_COMPONENTS
) -> pd.DataFrame:
    """各構成要素の生値列を正規化し `n_<key>` 列として追加する。

    絶対尺度を持つ層（騒音・用途地域）だけ順位化を通さない。
    どちらを使ったかは meta.json 経由でブラウザへも配信する
    （画面に「この層は地域内順位ではない」と出せるようにするため）。
    """
    out = mesh_df.copy()
    for c in components:
        raw = out[c.key] if c.key in out.columns else pd.Series(0.0, index=out.index)
        if c.absolute is not None:
            out[f"n_{c.key}"] = absolute_normalize(raw, c.absolute)
        else:
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

    **キー順に足す。定義順ではない。**

    浮動小数の加算は順序で最後の桁が変わる。定義順のまま足すと、
    **画面の並び順を変えただけでスコアが動く**——実際、2026-08-04 に
    需要側の並びを変えた（駅を先頭へ）ときに 29 区画の優先度が 4 桁目で
    ずれ、18 区画の順位が最大 3 つ動いた（上位 100 は不変）。
    `config.py` の並び順は**画面の見やすさのために決める**もので、
    そこを触るたびに数値が動くのでは、並べ替えを検討することすらできない。

    キー順という固定の順序で足せば、定義順をどう変えても和は同じになる。
    **TypeScript 側（`web/src/score.ts` の weightedSum）も同じ順に
    並べ替えること**——片方だけ直すと parity_check が落ちる。
    `selftest` が「並び順を入れ替えても結果が完全一致すること」を検査する。
    """
    total = pd.Series(np.zeros(len(df)), index=df.index, dtype=float)
    total_abs_weight = 0.0
    for c in sorted(components, key=lambda c: c.key):
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

    **かつてはここに 4 番目の手順があった——「表示用に priority も
    パーセンタイル化する」。2026-08-03 に外した。**

    パーセンタイル順位は定義上どこでも等間隔（9,507 区画なら 1/9,507 刻み）
    なので、上位 40 区画が幅 0.0042 に収まる。これは上位が横並びだという
    意味ではなく、順位化が必ずそうするというだけである。実際、掛け算の
    生値で見ると上位 40 区画は 0.9901〜0.9510（幅 0.0391）と **9.3 倍**
    開いており、**1 順位あたりの落ち方は上位がいちばん急**
    （上位40位まで 0.0010/位、1000〜2000位は 0.00016/位）。
    **画面が「上位は全部 1.00」に見えていたのは、差が無いからではなく
    順位化が差を捨てていたからである。**

    これはこのモジュール自身が percentile_normalize に書いている警告
    （「順位化は分布の広さを捨てる」）が、合成の段階にも当たっていた
    ということでもある。同じ理由で騒音と用途地域を絶対尺度へ移しておきながら、
    最後にもう一度かけ直していた。

    **外しても順位は 1 つも動かない。** パーセンタイル変換は単調なので、
    生値で並べても順位化後で並べても順序は同一である。変わるのは
    「その数値が何を意味するか」と、地図の色が順位ではなく実際の
    落ち方を反映するようになること。

    残る 2 回（手順 2 の demand / load）はそのまま。外すと順位そのものが
    動くうえ、重みを変えたときに色のスケールが飛ぶ（docs/issues.md）。
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

    # 需要が 0（＝事業所も学校も駅も無い）なら掛け算で 0 になる。
    # 「需要 0 なら優先度 0」は掛け算モデルの肝で、selftest が検査している。
    # 順位化を通していた頃は zero_is_absence=True がその 0 を保っていた。
    out["priority_raw"] = np.power(out["demand"].clip(0, None), alpha) * np.power(
        out["load"].clip(0, None), beta
    )
    out["priority"] = out["priority_raw"]
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


def collinearity_report(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """VIF と主成分。**2 変数間の相関では見えない多重計上**を捕まえる。

    相関行列は「どの 2 つが似ているか」しか答えない。ところが
    このモデルで疑わしいのは **駅・混雑・精神科・用途地域が揃って
    「ターミナル性」という 1 つの現象を指しているのではないか**という
    形の重複で（docs/issues.md A2）、2 つずつ見ても 0.9 を超えないまま
    4 層で同じものを 4 回足していることがあり得る。

    VIF は「その層を他の全層から線形に予測できてしまう度合い」。
    5 を超えたら、その層が持っている情報の 8 割が他層に既に在る。

    主成分は「実質いくつの独立な軸で動いているか」。第 1 主成分の寄与率が
    高く、そこに同符号で複数の層が乗っていれば、それが多重計上の中身になる。

    Returns
    -------
    (VIF の表, 主成分の表)
    """
    cols = [f"n_{c.key}" for c in ALL_COMPONENTS if f"n_{c.key}" in df.columns]
    labels = {f"n_{c.key}": c.label for c in ALL_COMPONENTS}
    X = df[cols].to_numpy(dtype=float)

    # --- VIF ---
    rows = []
    for i, col in enumerate(cols):
        y = X[:, i]
        others = np.delete(X, i, axis=1)
        # 切片つきの最小二乗。決定係数から VIF を出す。
        A = np.column_stack([np.ones(len(others)), others])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ coef
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 0.0
        vif = float("inf") if r2 >= 1.0 else 1.0 / (1.0 - r2)
        rows.append({"構成要素": labels[col], "R2": round(r2, 3), "VIF": round(vif, 2)})
    vif_df = pd.DataFrame(rows).sort_values("VIF", ascending=False, ignore_index=True)

    # --- 主成分（相関行列の固有分解）---
    # 標準化してから固有分解する。単位が違う層を混ぜないため。
    sd = X.std(axis=0, ddof=1)
    sd[sd == 0] = 1.0
    Z = (X - X.mean(axis=0)) / sd
    corr = np.corrcoef(Z, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0)
    vals, vecs = np.linalg.eigh(corr)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    ratio = vals / vals.sum()

    n_show = min(3, len(cols))
    pc = pd.DataFrame(
        {f"PC{j + 1}": np.round(vecs[:, j], 3) for j in range(n_show)},
        index=[labels[c] for c in cols],
    )
    pc.loc["── 寄与率"] = [round(float(ratio[j]), 3) for j in range(n_show)]
    pc.loc["── 累積"] = [round(float(ratio[: j + 1].sum()), 3) for j in range(n_show)]
    return vif_df, pc
