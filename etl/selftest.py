"""
パイプラインの不変条件を検証する。

    python -m etl.selftest

pytest 等に依存せず単体で走る。地理計算とスコア正規化は
「動いているように見えて静かに間違っている」種類のコードなので、
外形的な出力ではなく数学的な性質そのものを検査する。

TypeScript 側との式の一致は tools/parity_check.mjs が担当する。
"""

from __future__ import annotations

import sys
import traceback

import numpy as np
import pandas as pd

from . import mesh as meshlib
from . import score
from .aggregate import join_by_mesh_code, build_mesh_frame
from .config import ALL_COMPONENTS, DEMAND_COMPONENTS

_failures: list[str] = []


def check(name: str):
    """テスト関数を登録して実行するデコレータ代わりの薄いラッパ。"""

    def wrap(fn):
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as e:  # noqa: BLE001 — 失敗を集約して最後に報告する
            print(f"  FAIL  {name}: {e}")
            _failures.append(f"{name}: {e}\n{traceback.format_exc()}")
        return fn

    return wrap


# ---------------------------------------------------------------------------
# 地域メッシュ
# ---------------------------------------------------------------------------

# 実在の地点と、その 3 次メッシュコード（JIS X 0410）。
KNOWN_MESHES = [
    ("渋谷駅", 35.6580, 139.7016, "53393586"),
    ("東京駅", 35.6812, 139.7671, "53394611"),
    ("新宿都庁", 35.6896, 139.6917, "53394525"),
]


@check("メッシュコードが既知の地点と一致する")
def _known_codes():
    for name, lat, lon, expected in KNOWN_MESHES:
        got = meshlib.encode(lat, lon, 3)
        assert got == expected, f"{name}: {got} != {expected}"


@check("全次数で、符号化したセルが元の点を含む")
def _containment():
    for _, lat, lon, _ in KNOWN_MESHES:
        for level in (1, 2, 3, 4, 5):
            cell = meshlib.decode(meshlib.encode(lat, lon, level))
            assert cell.min_lat <= lat <= cell.max_lat, f"lat 外れ level={level}"
            assert cell.min_lon <= lon <= cell.max_lon, f"lon 外れ level={level}"


@check("セル重心を再符号化すると同じコードに戻る")
def _roundtrip():
    bbox = (139.60, 35.60, 139.72, 35.68)
    cells = list(meshlib.iter_bbox(bbox, 5))
    assert len(cells) > 500, f"セル数が少なすぎる: {len(cells)}"
    for c in cells:
        lon, lat = c.center
        assert meshlib.encode(lat, lon, 5) == c.code, f"往復不一致: {c.code}"


@check("メッシュ列挙に重複と抜けが無い")
def _grid_complete():
    bbox = (139.60, 35.60, 139.66, 35.64)
    cells = list(meshlib.iter_bbox(bbox, 5))
    codes = [c.code for c in cells]
    assert len(codes) == len(set(codes)), "重複したメッシュがある"

    # 南西端の格子が完全な矩形を成すこと（欠けがあれば積と一致しない）。
    lats = {round(c.min_lat, 8) for c in cells}
    lons = {round(c.min_lon, 8) for c in cells}
    assert len(cells) == len(lats) * len(lons), (
        f"格子に欠けがある: {len(cells)} != {len(lats)}×{len(lons)}"
    )


@check("上位次数への切り出しが整合する")
def _parent():
    code5 = meshlib.encode(35.6580, 139.7016, 5)
    assert meshlib.parent(code5, 3) == "53393586"
    assert meshlib.parent(code5, 1) == "5339"


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------


@check("不在(0)は厳密に 0 のまま、正の値だけが順位付けされる")
def _zero_is_absence():
    v = pd.Series([0.0, 0.0, 0.0, 1.0, 5.0, 100.0])
    n = score.percentile_normalize(v, zero_is_absence=True)
    assert (n[:3] == 0.0).all(), f"0 が 0 になっていない: {n.tolist()}"
    assert n.iloc[3] > 0.0, "最小の正値が 0 と区別されていない"
    assert n.iloc[5] == 1.0, f"最大値が 1.0 でない: {n.iloc[5]}"
    assert n.is_monotonic_increasing


@check("連続量は 0〜1 の全域に配分される")
def _continuous():
    v = pd.Series([10.0, 20.0, 30.0, 40.0])
    n = score.percentile_normalize(v, zero_is_absence=False)
    assert n.iloc[0] == 0.0 and n.iloc[-1] == 1.0, n.tolist()


@check("同値は平均順位を共有する")
def _ties():
    v = pd.Series([5.0, 5.0, 5.0, 9.0])
    n = score.percentile_normalize(v, zero_is_absence=True)
    assert n.iloc[0] == n.iloc[1] == n.iloc[2], f"同値が割れている: {n.tolist()}"
    assert n.iloc[3] == 1.0


@check("定数列は順位差を生まない")
def _constant():
    # 連続量として扱う場合、順位に情報が無いので全体を 0 に潰す。
    n = score.percentile_normalize(pd.Series([7.0] * 5), zero_is_absence=False)
    assert (n == 0.0).all(), n.tolist()

    # 不在扱いの場合は平均順位が一律に割り当たる（5 件なら 3/5 = 0.6）。
    # 値そのものは平均順位の定義から決まる任意の定数であって意味を持たない。
    # 重要なのは (a) 全メッシュで同一であること＝偽の順位差を作らないこと、
    # (b) 0 より大きいこと＝掛け算モデルで存在が消えないこと、
    # (c) TypeScript 側と同じ値になること（tools/parity_check.mjs が検証）。
    n = score.percentile_normalize(pd.Series([7.0] * 5), zero_is_absence=True)
    assert n.nunique() == 1, f"定数列から順位差が生まれている: {n.tolist()}"
    assert 0.0 < n.iloc[0] <= 1.0, n.tolist()

    # 0 が混ざれば、そこだけが厳密に 0 として分離される。
    mixed = score.percentile_normalize(
        pd.Series([0.0, 7.0, 7.0]), zero_is_absence=True
    )
    assert mixed.iloc[0] == 0.0 and mixed.iloc[1] == mixed.iloc[2] > 0.0, mixed.tolist()


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------


def _toy_frame(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    df = pd.DataFrame({"mesh_code": [f"{5339358600 + i}" for i in range(n)]})
    for c in ALL_COMPONENTS:
        df[c.key] = rng.random(n)
    return df


@check("需要が 0 のメッシュは優先度も 0 になる（掛け算モデルの核心）")
def _multiplicative():
    df = _toy_frame()
    # 先頭 5 件の需要側レイヤーをすべて 0 にする＝「誰も通わない場所」
    for c in DEMAND_COMPONENTS:
        df.loc[:4, c.key] = 0.0
    out = score.compose(score.normalize_components(df))
    assert (out.loc[:4, "demand"] == 0.0).all(), "需要が 0 になっていない"
    assert (out.loc[:4, "priority_raw"] == 0.0).all(), (
        "需要 0 のメッシュに優先度が付いている。足し算になっていないか確認すること"
    )
    assert out["priority_raw"].max() > 0.0, "全メッシュが 0 になっている"


@check("片側の重みを全て 0 にしても優先度が消えない")
def _neutral_side():
    df = score.normalize_components(_toy_frame())
    weights = {c.key: (0.0 if c.side == "load" else c.weight) for c in ALL_COMPONENTS}
    out = score.compose(df, weights)
    assert (out["load"] == 1.0).all(), "負荷側が中立(1.0)になっていない"
    assert out["priority_raw"].max() > 0.0, "優先度が全滅している"


@check("減点レイヤーは優先度を下げる向きに効く")
def _negative_sign():
    df = _toy_frame()
    df["green"] = 0.0
    base = score.compose(score.normalize_components(df))["load"].mean()
    df["green"] = np.linspace(0.1, 1.0, len(df))
    with_green = score.compose(score.normalize_components(df))
    # 緑被覆が最大のメッシュは、最小のメッシュより負荷が低くなるはず。
    lo = with_green.loc[with_green["green"].idxmax(), "load"]
    hi = with_green.loc[with_green["green"].idxmin(), "load"]
    assert lo < hi, f"緑地が負荷を下げていない: {lo} >= {hi}"
    assert base == base  # 基準値の算出自体が例外を投げないことの確認


# ---------------------------------------------------------------------------
# メッシュコード結合
# ---------------------------------------------------------------------------


@check("粗いメッシュ統計を細かいメッシュへ按分しても総量が保存される")
def _population_split():
    bbox = (139.70, 35.65, 139.72, 35.67)
    mesh_gdf = build_mesh_frame(bbox, 5)

    # 3 次メッシュ(1km)の人口表を作る。
    codes3 = sorted({c[:8] for c in mesh_gdf["mesh_code"]})
    table = pd.DataFrame({"mesh_code": codes3, "pop": [16000.0] * len(codes3)})

    got = join_by_mesh_code(mesh_gdf, table, "mesh_code", "pop")

    # 5 次メッシュは 3 次メッシュを 16 分割したもの。1 セルあたり 1/16。
    assert np.allclose(got.to_numpy(), 16000.0 / 16.0), (
        f"按分後の値が想定と違う: {sorted(set(got.round(3)))}"
    )

    # 研究領域が 3 次メッシュを完全に覆う場合、総量が保存されていること。
    covered = {c[:8] for c in mesh_gdf["mesh_code"]}
    full = [c for c in covered if sum(1 for m in mesh_gdf["mesh_code"] if m[:8] == c) == 16]
    if full:
        total = got[[m[:8] in full for m in mesh_gdf["mesh_code"]]].sum()
        assert np.isclose(total, 16000.0 * len(full)), (
            f"総量が保存されていない: {total} != {16000.0 * len(full)}"
        )


def main() -> int:
    print("calmgap-tokyo セルフテスト\n")
    # モジュール読み込み時に @check が実行済み。
    if _failures:
        print(f"\n{len(_failures)} 件失敗\n")
        for f in _failures:
            print(f)
        return 1
    print("\nすべて通過\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
