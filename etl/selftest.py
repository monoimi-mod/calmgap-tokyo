"""
パイプラインの不変条件を検証する。

    python -m etl.selftest

pytest 等に依存せず単体で走る。地理計算とスコア正規化は
「動いているように見えて静かに間違っている」種類のコードなので、
外形的な出力ではなく数学的な性質そのものを検査する。

TypeScript 側との式の一致は tools/parity_check.mjs が担当する。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import fetch
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


@check("配信精度への丸めが JavaScript の Math.round と一致する")
def _publish_round_matches_js():
    # ちょうど半分の値。Python の組み込み round は偶数側（0.0312）へ、
    # JavaScript の Math.round は大きい側（0.0313）へ丸める。
    # ここが食い違うと、地図の初期表示とスライダー既定値の色がズレる。
    assert score.publish_round(0.03125) == 0.0313, score.publish_round(0.03125)
    assert score.publish_round(0.00005) == 0.0001, score.publish_round(0.00005)
    assert score.publish_round(0.12344) == 0.1234
    got = score.publish_round(pd.Series([0.03125, 0.5, 0.0]))
    assert list(got) == [0.0313, 0.5, 0.0], list(got)


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


# ---------------------------------------------------------------------------
# 列の特定（決め打ちが外れたときに止まること）
# ---------------------------------------------------------------------------

# A29 で実際に起きた事故——列名の決め打ちが外れ、全件 NaN のまま既定値が
# 乗り、**ビルドもテストも成功と表示された**——を他のレイヤーで再発させない。
# ここで検査するのは「正しく動くこと」ではなく **間違いが黙って通らないこと**。
#
# 実データが手元に無くても検査できる。列名を故意にずらした小さな入力を作り、
# 正規化が例外で止まるかどうかを見ればよい。

# 渋谷駅前。研究領域（行政界から導出）の内側であることが前提。
_INSIDE = (35.6580, 139.7016)


def _tmp_geojson(tmp: Path, name: str, rows: list[dict]) -> Path:
    """点データの GeoJSON を書き出す。lat/lon を除いた列がそのまま属性になる。"""
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k not in ("lat", "lon")} for r in rows],
        geometry=[Point(r["lon"], r["lat"]) for r in rows],
        crs="EPSG:4326",
    )
    path = tmp / name
    gdf.to_file(path, driver="GeoJSON")
    return path


def _tmp_csv(tmp: Path, name: str, rows: list[dict]) -> Path:
    path = tmp / name
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


@contextlib.contextmanager
def _quiet():
    """正規化の進捗表示を伏せる。テストの合否だけを読めるようにするため。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield buf


def _raises(fn, *, contains: str) -> str:
    """fn が例外で止まり、その説明に手掛かりが入っていることを確かめる。"""
    try:
        with _quiet():
            fn()
    except Exception as e:  # noqa: BLE001 — 例外の型ではなく止まることが要件
        msg = str(e)
        assert contains in msg, f"説明に {contains!r} が無い:\n{msg}"
        return msg
    raise AssertionError("止まらずに通ってしまった（既定値で埋めていないか確認）")


@check("P29: 学校分類コードの列を特定できなければ止まる")
def _p29_requires_class_code():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 列名が仕様と違い、値も分類コードとして解釈できない。
        path = _tmp_geojson(
            tmp,
            "p29.geojson",
            [
                {"lat": lat, "lon": lon, "学校名": "都立◯◯学校", "生徒数": 200},
                {"lat": lat, "lon": lon + 0.002, "学校名": "都立△△学校", "生徒数": 300},
            ],
        )
        _raises(lambda: fetch.normalize_schools(path), contains="学校分類コード")


@check("P29: 特別支援学校のコードが 1 件も無ければ止まる")
def _p29_requires_special_needs_rows():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # P29_004 は在るが小中高だけ。== 16 で絞ると 0 件になる。
        # 0 件のレイヤーは実データとして数えられたまま需要を消してしまう。
        rows = [
            {"lat": lat, "lon": lon + i * 0.002, "P29_004": code, "P29_009": 300}
            for i, code in enumerate([1, 2, 3, 1, 2])
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="1 件も無い")


@check("P29: 生徒数の列が無ければ止まり、--assume-missing でだけ通る")
def _p29_students():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"lat": lat, "lon": lon + i * 0.002, "P29_004": 16, "P29_005": f"支援校{i}"}
            for i in range(3)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="児童生徒数")

        with _quiet():
            out = fetch.normalize_schools(path, assume_missing=True)
        assert len(out) == 3, f"3 件のはずが {len(out)} 件"
        assert (out["capacity"] == fetch.SCHOOL_STUDENTS_FALLBACK).all(), (
            "既定値が入っていない"
        )


@check("P14: 施設名称の列を特定できなければ止まる（都道府県名の列を掴まない）")
def _p14_requires_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称の列が無い。文字列であることだけを条件に探すと
        # P14_001（"東京都"）を名称と誤認する——実際に一度そうなった。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P14_001": "東京都",
                "P14_002": "渋谷区",
                "P14_007": code,
            }
            for i, code in enumerate(["030300", "050901"])
        ]
        path = _tmp_geojson(tmp, "p14.geojson", rows)
        _raises(lambda: fetch.normalize_welfare(path), contains="施設名称")


@check("P14: 小分類コードと名称がそろえば障害福祉事業所を抽出できる")
def _p14_happy_path():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {
                "lat": lat,
                "lon": lon,
                "P14_007": "030300",
                "P14_008": "障害者支援施設あおぞら",
            },
            {
                "lat": lat,
                "lon": lon + 0.002,
                "P14_007": "050901",
                "P14_008": "児童発達支援センターにじ",
            },
        ]
        path = _tmp_geojson(tmp, "p14.geojson", rows)
        with _quiet():
            out = fetch.normalize_welfare(path)
        assert len(out) == 2, f"2 件のはずが {len(out)} 件"
        # 定員は推定値であることが列として残っていること（提言時の但し書きの根拠）。
        assert out["capacity_estimated"].all(), "推定であることが失われている"
        assert (out["demand_value"] > 0).all(), out["demand_value"].tolist()


@check("P29: 生徒数らしい数値の列があっても、名前の裏付けが無ければ拾わない")
def _p29_students_needs_name_hint():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 「建築年」は児童生徒数と値域が重なる。中身だけで当てようとすると
        # これを生徒数として拾い、規模の重み付けが無意味な値で回る。
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_004": 16,
                "P29_005": f"支援校{i}",
                "建築年": 1975 + i * 5,
            }
            for i in range(6)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        _raises(lambda: fetch.normalize_schools(path), contains="児童生徒数")


@check("P29: 列名が違っても、名前に手掛かりがあれば生徒数を特定する")
def _p29_students_detected_with_hint():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {
                "lat": lat,
                "lon": lon + i * 0.002,
                "P29_004": 16,
                "P29_005": f"支援校{i}",
                "在籍者数": 100 + i * 30,
            }
            for i in range(6)
        ]
        path = _tmp_geojson(tmp, "p29.geojson", rows)
        with _quiet():
            out = fetch.normalize_schools(path)
        assert sorted(out["capacity"]) == [100, 130, 160, 190, 220, 250], (
            out["capacity"].tolist()
        )


@check("P04: 診療科目の列を特定できなければ止まる（全医療機関の混入を防ぐ）")
def _p04_requires_departments():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"lat": lat, "lon": lon + i * 0.002, "P04_002": f"◯◯病院{i}", "備考": ""}
            for i in range(3)
        ]
        path = _tmp_geojson(tmp, "p04.geojson", rows)
        _raises(lambda: fetch.normalize_clinics(path), contains="診療科目")


@check("P04: 列名が違っても中身から診療科目を特定し、絞り込みが効く")
def _p04_detects_departments_by_value():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 列名 P04_003 ではない。値だけが診療科目であることを示している。
        rows = [
            {"lat": lat, "lon": lon, "P04_002": "Aクリニック", "診療科": "精神科・心療内科"},
            {"lat": lat, "lon": lon + 0.002, "P04_002": "B医院", "診療科": "内科・小児科"},
            {"lat": lat, "lon": lon + 0.004, "P04_002": "C病院", "診療科": "外科・整形外科"},
        ]
        path = _tmp_geojson(tmp, "p04.geojson", rows)
        with _quiet():
            out = fetch.normalize_clinics(path)
        assert len(out) == 1, f"精神科 1 件だけ残るはずが {len(out)} 件"
        assert out.iloc[0]["name"] == "Aクリニック", out.iloc[0]["name"]


@check("騒音: 昼夜どちらの dB 列もあるとき昼間を選ぶ")
def _noise_prefers_daytime():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # どちらも列名は候補に無く、値域も同じ。名前の手掛かりだけが違う。
        rows = [
            {
                "緯度": lat,
                "経度": lon + i * 0.002,
                "昼間LAeq(dB)": 70 + i,
                "夜間LAeq(dB)": 60 + i,
            }
            for i in range(6)
        ]
        path = _tmp_csv(tmp, "noise.csv", rows)
        with _quiet():
            out = fetch.normalize_noise(path)
        assert out["laeq_db"].min() >= 70.0, (
            f"夜間の列を拾っている: {sorted(out['laeq_db'].unique())}"
        )


@check("騒音: 騒音レベルの列を特定できなければ止まる")
def _noise_requires_laeq():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"緯度": lat, "経度": lon + i * 0.002, "地点番号": 1000 + i}
            for i in range(6)
        ]
        path = _tmp_csv(tmp, "noise.csv", rows)
        _raises(lambda: fetch.normalize_noise(path), contains="等価騒音レベル")


@check("公共施設: 施設名から種別を判定できなければ止まる（到達不可の過大計上を防ぐ）")
def _facilities_requires_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        # 名称の列が「施設コード」しかない。従来は候補 0 件のまま通り、
        # 「既存施設では到達不可」が全メッシュで成立していた。
        rows = [
            {"緯度": lat, "経度": lon + i * 0.002, "施設コード": f"S{i:03d}"}
            for i in range(4)
        ]
        path = _tmp_csv(tmp, "facilities.csv", rows)
        _raises(lambda: fetch.normalize_hosts(path), contains="施設名")


@check("公共施設: 列名が違っても中身から施設名を特定し、対象外施設だけを落とす")
def _facilities_detects_name_by_value():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        lat, lon = _INSIDE
        rows = [
            {"緯度": lat, "経度": lon, "名前": "渋谷区立中央図書館"},
            {"緯度": lat, "経度": lon + 0.002, "名前": "◯◯区民センター"},
            {"緯度": lat, "経度": lon + 0.004, "名前": "△△保育園"},
        ]
        path = _tmp_csv(tmp, "facilities.csv", rows)
        with _quiet():
            out = fetch.normalize_hosts(path)
        kinds = set(out["host_kind"])
        assert len(out) == 2, f"図書館と区民センターの 2 件のはずが {len(out)} 件"
        assert "図書館" in kinds, kinds


# ---------------------------------------------------------------------------
# 複数出典が同じレイヤーへ書くとき
# ---------------------------------------------------------------------------

# hosts は p14-hosts（児童館）と facilities（区の公共施設一覧）が、
# welfare は p14 と wamnet が書く。あとから流した方が既存を丸ごと
# 置き換えるため、児童館 154 件を消したことに気付かないまま
# 「既存施設では到達不可」が増える——という壊れ方をしていた。


def _hosts_frame(rows: list[tuple[str, float, float, str]]) -> gpd.GeoDataFrame:
    from shapely.geometry import Point

    return gpd.GeoDataFrame(
        {
            "name": [r[0] for r in rows],
            "host_kind": ["児童館"] * len(rows),
            "source": [r[3] for r in rows],
        },
        geometry=[Point(r[2], r[1]) for r in rows],
        crs="EPSG:4326",
    )


@check("既存の出典が消えるなら、指定が無い限り書かずに止まる")
def _merge_requires_choice():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hosts.geojson"
        _hosts_frame([("A児童館", 35.658, 139.701, "P14")]).to_file(
            out, driver="GeoJSON"
        )
        new = _hosts_frame([("B図書館", 35.659, 139.702, "公共施設一覧")])

        try:
            with _quiet():
                fetch._merge_with_existing(
                    new, out, kind="facilities", layer_key="hosts", merge=None
                )
        except SystemExit as e:
            assert "--append" in str(e) and "P14" in str(e), str(e)
        else:
            raise AssertionError("既存の出典を黙って捨てた")

        # append なら両方残る。replace なら入れ替わる。
        with _quiet():
            merged = fetch._merge_with_existing(
                new, out, kind="facilities", layer_key="hosts", merge="append"
            )
            replaced = fetch._merge_with_existing(
                new, out, kind="facilities", layer_key="hosts", merge="replace"
            )
        assert len(merged) == 2, len(merged)
        assert set(merged["source"]) == {"P14", "公共施設一覧"}, set(merged["source"])
        assert len(replaced) == 1, len(replaced)


@check("同じ出典を入れ直すだけなら止まらない")
def _merge_same_source_replaces():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "hosts.geojson"
        _hosts_frame([("A児童館", 35.658, 139.701, "P14")]).to_file(
            out, driver="GeoJSON"
        )
        new = _hosts_frame(
            [("A児童館", 35.658, 139.701, "P14"), ("B児童館", 35.66, 139.70, "P14")]
        )
        with _quiet():
            got = fetch._merge_with_existing(
                new, out, kind="p14-hosts", layer_key="hosts", merge=None
            )
        assert len(got) == 2, len(got)


@check("同名で近い施設は 1 件に寄せ、同名でも離れていれば残す")
def _dedupe_by_name_and_distance():
    # 区の一覧と P14 で同じ児童館。名称に全角空白、座標は 44m ずれ。
    # 座標を丸めて格子で寄せる実装ではこれを取り逃がした（格子の境目）。
    rows = [
        ("四番町児童館", 35.68830, 139.73660, "P14"),
        ("四番　町児童館", 35.68870, 139.73660, "公共施設一覧"),
        # 同名だが 1km 以上離れた別施設（分館など）は残す。
        ("四番町児童館", 35.70000, 139.73660, "公共施設一覧"),
    ]
    with _quiet():
        got = fetch.dedupe_points(_hosts_frame(rows), tag="test")
    assert len(got) == 2, [n for n in got["name"]]
    assert got.iloc[0]["source"] == "P14", "先に入っていた側を残していない"


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
