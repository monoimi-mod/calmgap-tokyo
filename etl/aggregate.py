"""
点・線・面のデータを標準地域メッシュへ集計する。

本プロジェクトの技術的な核。オープンデータは
「点（事業所・騒音測定点）」「面（用途地域・公園）」「メッシュ（人口）」が
混在しており、これらを同一のメッシュ土俵に載せなければスコア化できない。

距離計算・面積計算はすべて平面直角座標系（EPSG:6677）で行う。
緯度経度のまま距離を測ると東京では経度方向が約 18% 過大に出る。
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Polygon

from . import mesh as meshlib
from .config import CRS_GEOGRAPHIC, CRS_PROJECTED


# ---------------------------------------------------------------------------
# メッシュ GeoDataFrame の生成
# ---------------------------------------------------------------------------


def build_mesh_frame(
    bbox: tuple[float, float, float, float],
    level: int,
    clip: gpd.GeoDataFrame | None = None,
) -> gpd.GeoDataFrame:
    """研究領域を覆うメッシュの GeoDataFrame を作る。

    clip に行政界を渡すと、その内側に重心を持つメッシュだけを残す。
    重心判定にするのは、境界に跨るメッシュを二重計上しないため。
    """
    cells = list(meshlib.iter_bbox(bbox, level))
    gdf = gpd.GeoDataFrame(
        {
            "mesh_code": [c.code for c in cells],
            "lon": [c.center[0] for c in cells],
            "lat": [c.center[1] for c in cells],
        },
        geometry=[Polygon(c.polygon_coords()) for c in cells],
        crs=CRS_GEOGRAPHIC,
    )

    if clip is not None and len(clip):
        clip = clip.to_crs(CRS_GEOGRAPHIC)
        centroids = gpd.GeoDataFrame(
            gdf[["mesh_code"]],
            geometry=gpd.points_from_xy(gdf["lon"], gdf["lat"]),
            crs=CRS_GEOGRAPHIC,
        )
        hit = gpd.sjoin(centroids, clip[["geometry"]], predicate="within", how="inner")
        gdf = gdf[gdf["mesh_code"].isin(hit["mesh_code"])].copy()

    return gdf.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 点 → メッシュ（距離減衰カーネル）
# ---------------------------------------------------------------------------


def _xy(gdf: gpd.GeoDataFrame) -> np.ndarray:
    """投影座標の (N,2) 配列を返す。面が来た場合は重心を使う。"""
    g = gdf.to_crs(CRS_PROJECTED).geometry
    if not (g.geom_type == "Point").all():
        g = g.centroid
    return np.column_stack([g.x.to_numpy(), g.y.to_numpy()])


def points_to_mesh(
    mesh_gdf: gpd.GeoDataFrame,
    points: gpd.GeoDataFrame,
    value_col: str | None = None,
    bandwidth_m: float = 800.0,
    cutoff_factor: float = 2.0,
) -> pd.Series:
    """点データをガウシアンカーネルでメッシュへ配分する。

    単純な「メッシュ内に落ちた点だけを数える」集計を採らない理由:
    250m メッシュに対して徒歩圏は 800m あり、隣接メッシュにある事業所も
    そのメッシュの需要を確かに生んでいる。境界のどちら側に立っているかで
    スコアが不連続に変わるのは、提言の根拠として弱い。

    重み w(d) = exp(-0.5 * (d / bandwidth)^2)、d > cutoff*bandwidth は 0。

    Returns
    -------
    mesh_gdf と同じ順序・長さの Series（インデックスは mesh_code）。
    """
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if points is None or len(points) == 0:
        return pd.Series(np.zeros(len(mesh_gdf)), index=index, dtype=float)

    mesh_xy = _xy(mesh_gdf)
    pt_xy = _xy(points)
    values = (
        points[value_col].fillna(0).to_numpy(dtype=float)
        if value_col
        else np.ones(len(points), dtype=float)
    )

    cutoff = bandwidth_m * cutoff_factor
    out = np.zeros(len(mesh_gdf), dtype=float)

    # メッシュ数 × 点数 が大きくなりすぎないようブロック処理する。
    # 5次メッシュ 2 区分（約 5,000）× 事業所（約 1,000）程度なら全く問題ない規模だが、
    # 23 区拡張（Phase 2）でも同じコードが動くようにしておく。
    block = max(1, int(4_000_000 / max(len(pt_xy), 1)))
    for start in range(0, len(mesh_xy), block):
        chunk = mesh_xy[start : start + block]
        d2 = (
            (chunk[:, 0][:, None] - pt_xy[None, :, 0]) ** 2
            + (chunk[:, 1][:, None] - pt_xy[None, :, 1]) ** 2
        )
        w = np.exp(-0.5 * d2 / (bandwidth_m**2))
        w[d2 > cutoff**2] = 0.0
        out[start : start + block] = w @ values

    return pd.Series(out, index=index, dtype=float)


def nearest_distance(
    mesh_gdf: gpd.GeoDataFrame, targets: gpd.GeoDataFrame
) -> pd.Series:
    """各メッシュ重心から最寄りターゲットまでの距離(m)。

    鉄道路線からの近接負荷など「最も近い 1 件だけが効く」レイヤーに使う。
    """
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if targets is None or len(targets) == 0:
        return pd.Series(np.full(len(mesh_gdf), np.inf), index=index, dtype=float)

    mesh_pts = mesh_gdf.to_crs(CRS_PROJECTED).geometry.centroid
    tgt = targets.to_crs(CRS_PROJECTED)
    union = tgt.geometry.union_all()
    return pd.Series(
        mesh_pts.distance(union).to_numpy(dtype=float), index=index, dtype=float
    )


def proximity_score(
    distance_m: pd.Series, half_distance_m: float = 300.0
) -> pd.Series:
    """距離を 0〜1 の近接スコアへ変換する。half_distance で 0.5 になる。"""
    d = distance_m.replace([np.inf, -np.inf], np.nan).fillna(1e9)
    return 1.0 / (1.0 + (d / half_distance_m) ** 2)


def idw_to_mesh(
    mesh_gdf: gpd.GeoDataFrame,
    points: gpd.GeoDataFrame,
    value_col: str,
    power: float = 2.0,
    max_distance_m: float = 1500.0,
    smoothing_m: float = 50.0,
) -> pd.Series:
    """点の実測値を逆距離加重（IDW）でメッシュへ内挿する。

    騒音の要請限度測定は「点でしか測っていない」。
    これを面に変換するのが本プロジェクトの技術的主張のひとつ。
    max_distance より遠い点しかないメッシュは NaN（＝観測なし）を返し、
    score 側で中央値補完する。0 で埋めると「静かな場所」と誤認されるため。
    """
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if points is None or len(points) == 0:
        return pd.Series(np.full(len(mesh_gdf), np.nan), index=index, dtype=float)

    mesh_xy = _xy(mesh_gdf)
    pt_xy = _xy(points)
    values = points[value_col].to_numpy(dtype=float)
    ok = ~np.isnan(values)
    pt_xy, values = pt_xy[ok], values[ok]
    if len(values) == 0:
        return pd.Series(np.full(len(mesh_gdf), np.nan), index=index, dtype=float)

    d = np.sqrt(
        (mesh_xy[:, 0][:, None] - pt_xy[None, :, 0]) ** 2
        + (mesh_xy[:, 1][:, None] - pt_xy[None, :, 1]) ** 2
    )
    # smoothing_m を足して、測定点直上での 0 除算と極端なスパイクを避ける。
    w = 1.0 / np.power(d + smoothing_m, power)
    w[d > max_distance_m] = 0.0

    denom = w.sum(axis=1)
    out = np.where(denom > 0, (w @ values) / np.where(denom > 0, denom, 1.0), np.nan)
    return pd.Series(out, index=index, dtype=float)


# ---------------------------------------------------------------------------
# 面 → メッシュ（面積加重）
# ---------------------------------------------------------------------------


def polygon_area_weighted_mean(
    mesh_gdf: gpd.GeoDataFrame,
    polygons: gpd.GeoDataFrame,
    value_col: str,
    default: float = 0.0,
) -> pd.Series:
    """メッシュに掛かるポリゴンの値を、重なり面積で加重平均する。

    用途地域のように 1 メッシュが複数区分に跨る場合、
    重心が乗った 1 区分で代表させると境界メッシュの評価を誤る。
    """
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if polygons is None or len(polygons) == 0:
        return pd.Series(np.full(len(mesh_gdf), default), index=index, dtype=float)

    m = mesh_gdf[["mesh_code", "geometry"]].to_crs(CRS_PROJECTED)
    p = polygons[[value_col, "geometry"]].to_crs(CRS_PROJECTED)

    inter = gpd.overlay(m, p, how="intersection", keep_geom_type=True)
    if len(inter) == 0:
        return pd.Series(np.full(len(mesh_gdf), default), index=index, dtype=float)

    inter["_area"] = inter.geometry.area
    inter["_wv"] = inter["_area"] * inter[value_col].astype(float)
    g = inter.groupby("mesh_code").agg(wv=("_wv", "sum"), a=("_area", "sum"))
    mean = (g["wv"] / g["a"]).rename("v")

    return (
        pd.Series(index.map(mean), index=index, dtype=float).fillna(default)
    )


def polygon_coverage_ratio(
    mesh_gdf: gpd.GeoDataFrame, polygons: gpd.GeoDataFrame
) -> pd.Series:
    """メッシュ面積に占めるポリゴンの被覆率 0〜1。公園・緑被覆に使う。"""
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if polygons is None or len(polygons) == 0:
        return pd.Series(np.zeros(len(mesh_gdf)), index=index, dtype=float)

    m = mesh_gdf[["mesh_code", "geometry"]].to_crs(CRS_PROJECTED)
    m["_mesh_area"] = m.geometry.area
    # 重なり合うポリゴン（公園の多重登録）で 100% を超えないよう先に融合する。
    p = gpd.GeoDataFrame(
        geometry=[polygons.to_crs(CRS_PROJECTED).geometry.union_all()],
        crs=CRS_PROJECTED,
    )

    inter = gpd.overlay(m, p, how="intersection", keep_geom_type=True)
    if len(inter) == 0:
        return pd.Series(np.zeros(len(mesh_gdf)), index=index, dtype=float)

    inter["_a"] = inter.geometry.area
    covered = inter.groupby("mesh_code")["_a"].sum()
    area = m.set_index("mesh_code")["_mesh_area"]
    ratio = (covered / area).clip(0.0, 1.0)

    return pd.Series(index.map(ratio), index=index, dtype=float).fillna(0.0)


# ---------------------------------------------------------------------------
# メッシュコード直結合
# ---------------------------------------------------------------------------


def join_by_mesh_code(
    mesh_gdf: gpd.GeoDataFrame,
    table: pd.DataFrame,
    code_col: str,
    value_col: str,
    default: float = 0.0,
) -> pd.Series:
    """e-Stat の地域メッシュ統計をコードで直接結合する。

    統計側が粗い次数（3次=1km）の場合は、上位コードへ丸めて突き合わせ、
    値は子メッシュ数で等分する。人口は密度量であり、
    1km メッシュの値をそのまま 250m メッシュへ複製すると 16 倍に膨らむ。
    """
    index = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    if table is None or len(table) == 0:
        return pd.Series(np.full(len(mesh_gdf), default), index=index, dtype=float)

    t = table[[code_col, value_col]].copy()
    t[code_col] = t[code_col].astype(str).str.strip()
    src_len = int(t[code_col].str.len().mode().iloc[0])
    src_level = next(
        (lv for lv, n in meshlib.CODE_LENGTH.items() if n == src_len), None
    )
    if src_level is None:
        raise ValueError(f"結合元のメッシュコード桁数が不正: {src_len}")

    lookup = t.groupby(code_col)[value_col].sum()
    keys = index.to_series().str[:src_len]
    vals = keys.map(lookup).astype(float)

    # 分割数 = 4^(4次以降の差) × 100^(2→3次) 等。CELL_SIZE から面積比で求める。
    target_level = next(
        lv for lv, n in meshlib.CODE_LENGTH.items() if n == len(index[0])
    )
    if target_level > src_level:
        sa = meshlib.CELL_SIZE[src_level]
        ta = meshlib.CELL_SIZE[target_level]
        n_children = (sa[0] / ta[0]) * (sa[1] / ta[1])
        vals = vals / n_children

    return pd.Series(vals.to_numpy(), index=index, dtype=float).fillna(default)
