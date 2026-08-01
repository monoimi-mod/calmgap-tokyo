"""
オフライン用の模擬データ生成器。

⚠️ ここで作られるデータはすべて架空である。
    実在のオープンデータを 1 件も含まない。
    数値を根拠として引用してはならない。

存在理由:
    本リポジトリの ETL・スコアリング・地図 UI を、
    実データの取得可否と切り離して開発・検証できるようにするため。
    実データ投入は `python -m etl.build --live` に切り替えるだけで、
    以降のパイプラインは一切変更しなくてよい。

架空にする範囲:
    座標のアンカー（駅・公園の位置）は実際の地理を用いる。
    地理そのものは公知の事実であり、ここを架空にすると
    「上位メッシュが実際にキツい場所と一致するか」という
    サニティチェック（ハンドオフ 7.）の予行演習ができなくなるため。
    一方、乗降人員・定員・騒音値・施設名といった属性値はすべて生成物であり、
    全レコードが source="SYNTHETIC FIXTURE" と synthetic=True を持つ。
    施設名も実在名を騙らないよう「模擬」を冠する。
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon

from .config import CRS_GEOGRAPHIC, STUDY_BBOX
from .schema import ZONING_LOAD

SYNTHETIC_SOURCE = "SYNTHETIC FIXTURE"

# 実在する地理アンカー（座標のみ事実、属性は生成）。
# intensity は「その拠点がどれだけ都市的か」の 0〜1 の作り込みパラメータ。
_CENTERS: list[tuple[str, float, float, float]] = [
    # (名称, lon, lat, intensity)
    ("渋谷駅", 139.7016, 35.6580, 1.00),
    ("恵比寿駅", 139.7100, 35.6467, 0.62),
    ("原宿駅", 139.7027, 35.6702, 0.58),
    ("代々木上原駅", 139.6800, 35.6690, 0.34),
    ("三軒茶屋駅", 139.6712, 35.6434, 0.48),
    ("下北沢駅", 139.6680, 35.6613, 0.46),
    ("二子玉川駅", 139.6280, 35.6115, 0.44),
    ("成城学園前駅", 139.5990, 35.6404, 0.28),
    ("千歳烏山駅", 139.6010, 35.6690, 0.26),
    ("用賀駅", 139.6320, 35.6270, 0.22),
    ("経堂駅", 139.6350, 35.6510, 0.24),
    ("等々力駅", 139.6480, 35.6060, 0.18),
]

# 実在する大規模緑地（位置は事実、形状は円で近似した模擬ポリゴン）。
_PARKS: list[tuple[str, float, float, float]] = [
    ("代々木公園", 139.6949, 35.6720, 700.0),
    ("駒沢オリンピック公園", 139.6620, 35.6255, 620.0),
    ("砧公園", 139.6180, 35.6280, 640.0),
    ("羽根木公園", 139.6540, 35.6570, 260.0),
    ("蘆花恒春園", 139.6060, 35.6520, 300.0),
]

# 主要幹線道路の概略ライン（騒音測定点を並べる軸として使う）。
_ARTERIALS: list[tuple[str, list[tuple[float, float]]]] = [
    ("国道246号", [(139.7016, 35.6580), (139.6712, 35.6434), (139.6280, 35.6115)]),
    ("環状七号線", [(139.6680, 35.6800), (139.6620, 35.6300), (139.6700, 35.5980)]),
    ("環状八号線", [(139.6150, 35.6800), (139.6080, 35.6350), (139.6300, 35.5950)]),
    ("山手通り", [(139.6960, 35.6800), (139.6930, 35.6450), (139.6980, 35.6150)]),
]

_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 111_320.0 * np.cos(np.radians(35.65))


def _meters_to_deg(dx_m: float, dy_m: float) -> tuple[float, float]:
    return dx_m / _M_PER_DEG_LON, dy_m / _M_PER_DEG_LAT


def _urbanity(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """各地点の「都市らしさ」0〜1。各拠点からの距離減衰の最大値を採る。

    模擬データ全体に一貫した空間構造を与えるための下敷き。
    用途地域・混雑・騒音・駅勢圏をすべてこの場から派生させることで、
    互いに相関した現実的なデータになる（独立乱数だと
    スコアが空間的に無意味なノイズになり、地図として検証にならない）。
    """
    score = np.zeros(len(lon))
    for _, clon, clat, intensity in _CENTERS:
        dx = (lon - clon) * _M_PER_DEG_LON
        dy = (lat - clat) * _M_PER_DEG_LAT
        d = np.sqrt(dx**2 + dy**2)
        # 拠点の規模が大きいほど影響半径も広い
        radius = 500.0 + 1400.0 * intensity
        score = np.maximum(score, intensity * np.exp(-0.5 * (d / radius) ** 2))
    return np.clip(score, 0.0, 1.0)


def _sample_points(rng: np.random.Generator, n: int, urban_bias: float):
    """研究領域内に点を撒く。urban_bias が高いほど拠点周辺に寄る。"""
    minx, miny, maxx, maxy = STUDY_BBOX
    lons, lats = [], []
    while len(lons) < n:
        batch = max(n * 4, 64)
        clon = rng.uniform(minx, maxx, batch)
        clat = rng.uniform(miny, maxy, batch)
        u = _urbanity(clon, clat)
        keep = rng.random(batch) < (1 - urban_bias) + urban_bias * u
        lons.extend(clon[keep].tolist())
        lats.extend(clat[keep].tolist())
    return np.array(lons[:n]), np.array(lats[:n])


def _gdf(df: pd.DataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs=CRS_GEOGRAPHIC,
    )


# ---------------------------------------------------------------------------
# 需要レイヤー
# ---------------------------------------------------------------------------


def welfare_facilities(rng: np.random.Generator, n: int = 260) -> gpd.GeoDataFrame:
    """模擬 障害福祉サービス事業所。

    urban_bias を中程度にしてあるのは、実際の事業所が
    賃料の関係で駅前一等地を避け、住宅地寄りに分布する傾向を模したもの。
    """
    from .schema import (
        ASSUMED_ONLY_KINDS,
        WELFARE_DEMAND_WEIGHT,
        assumed_capacity,
        welfare_weight,
    )

    lon, lat = _sample_points(rng, n, urban_bias=0.45)
    kinds = rng.choice(
        list(WELFARE_DEMAND_WEIGHT.keys()),
        size=n,
        p=_kind_probabilities(list(WELFARE_DEMAND_WEIGHT.keys())),
    )
    capacity = np.clip(rng.lognormal(mean=2.9, sigma=0.6, size=n), 5, 120).round()
    # 実データでは訪問系・相談系に制度上定員が無く、種別ごとの仮定員で補う。
    # 模擬でも同じ割合で立てておかないと、**模擬モードだけ感度分析の
    # 「仮定員を落とす」群が空振りして 100% 維持と表示される**。
    assumed = np.isin(kinds, sorted(ASSUMED_ONLY_KINDS))
    # 仮定員の行は実データと同じく「種別ごとの仮定員そのもの」を入れる。
    # 乱数のままだと、感度分析が仮定員を 1.0 倍したときに元の値へ戻らず、
    # 基準との比較にならない。
    capacity = np.where(assumed, [assumed_capacity(k) for k in kinds], capacity)

    df = pd.DataFrame(
        {
            "name": [f"模擬事業所 W-{i:03d}" for i in range(n)],
            "kind": kinds,
            "capacity": capacity,
            "capacity_assumed": assumed,
            "lon": lon,
            "lat": lat,
            "source": SYNTHETIC_SOURCE,
            "synthetic": True,
        }
    )
    df["weight"] = [welfare_weight(k) for k in df["kind"]]
    # 需要への寄与 = 定員 × 種別重み
    df["demand_value"] = df["capacity"] * df["weight"]
    return _gdf(df)


def _kind_probabilities(keys: list[str]) -> np.ndarray:
    """事業所種別の出現比率。通所系が多く入所系が少ない実態に寄せる。"""
    common = {
        "放課後等デイサービス": 4.0,
        "就労継続支援B型": 3.5,
        "居宅介護": 3.0,
        "児童発達支援": 2.0,
        "計画相談支援": 2.0,
        "生活介護": 1.8,
        "就労移行支援": 1.4,
        "共同生活援助": 1.2,
        "就労継続支援A型": 0.9,
        "重度訪問介護": 0.8,
    }
    w = np.array([common.get(k, 0.4) for k in keys], dtype=float)
    return w / w.sum()


def special_schools(rng: np.random.Generator, n: int = 9) -> gpd.GeoDataFrame:
    """模擬 特別支援学校。実際も区内に数校程度しかない。"""
    lon, lat = _sample_points(rng, n, urban_bias=0.15)
    students = np.clip(rng.normal(180, 70, n), 45, 420).round()
    df = pd.DataFrame(
        {
            "name": [f"模擬特別支援学校 S-{i:02d}" for i in range(n)],
            "kind": "特別支援学校",
            "capacity": students,
            "weight": 1.0,
            "demand_value": students,
            "lon": lon,
            "lat": lat,
            "source": SYNTHETIC_SOURCE,
            # 実データ側と列をそろえる。模擬の在籍者数は乱数なので、
            # 「実数ではない」という意味では規模不明と同じ扱いにする。
            "students_assumed": True,
            "synthetic": True,
        }
    )
    return _gdf(df)


def clinics(rng: np.random.Generator, n: int = 70) -> gpd.GeoDataFrame:
    """模擬 精神科・心療内科。駅近に強く偏る実態を反映する。"""
    lon, lat = _sample_points(rng, n, urban_bias=0.85)
    df = pd.DataFrame(
        {
            "name": [f"模擬クリニック C-{i:03d}" for i in range(n)],
            "kind": "精神科・心療内科",
            "capacity": 1.0,
            "weight": 1.0,
            "demand_value": 1.0,
            "lon": lon,
            "lat": lat,
            "source": SYNTHETIC_SOURCE,
            "synthetic": True,
        }
    )
    return _gdf(df)


def stations(rng: np.random.Generator) -> gpd.GeoDataFrame:
    """模擬 駅乗降人員。座標と駅名は実在、人員数は生成値。"""
    rows = []
    for name, lon, lat, intensity in _CENTERS:
        # intensity をもとに桁を作り、±15% の揺らぎを乗せる
        base = 12_000 + intensity**2.2 * 620_000
        daily = float(base * rng.uniform(0.85, 1.15))
        rows.append(
            {
                "name": name,
                "kind": "駅",
                "capacity": round(daily),
                "weight": 1.0,
                "demand_value": round(daily),
                "lon": lon,
                "lat": lat,
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )
    return _gdf(pd.DataFrame(rows))


# ---------------------------------------------------------------------------
# 負荷レイヤー
# ---------------------------------------------------------------------------


def zoning(rng: np.random.Generator, cell_m: float = 250.0) -> gpd.GeoDataFrame:
    """模擬 用途地域。都市らしさから区分を割り当てた格子ポリゴン。

    実データは不定形ポリゴンだが、面積加重平均のロジック
    （aggregate.polygon_area_weighted_mean）は形状に依存しないため、
    格子で代替しても検証として成立する。
    """
    minx, miny, maxx, maxy = STUDY_BBOX
    dlon, dlat = _meters_to_deg(cell_m, cell_m)

    lons = np.arange(minx, maxx, dlon)
    lats = np.arange(miny, maxy, dlat)
    glon, glat = np.meshgrid(lons + dlon / 2, lats + dlat / 2)
    u = _urbanity(glon.ravel(), glat.ravel())
    noise = rng.normal(0, 0.07, u.shape)
    v = np.clip(u + noise, 0, 1)

    # 都市らしさの分位で用途地域コードへ写像する。
    # 商業地域は全体のごく一部にしか出ない、という現実の分布に合わせる。
    bins = [0.06, 0.14, 0.24, 0.34, 0.45, 0.56, 0.66, 0.78, 0.88, 0.94]
    codes = [1, 2, 3, 4, 5, 6, 7, 10, 8, 9, 9]
    idx = np.digitize(v, bins)
    zcode = np.array(codes, dtype=int)[idx]

    polys, recs = [], []
    for k, (clon, clat) in enumerate(zip(glon.ravel(), glat.ravel())):
        polys.append(
            Polygon(
                [
                    (clon - dlon / 2, clat - dlat / 2),
                    (clon + dlon / 2, clat - dlat / 2),
                    (clon + dlon / 2, clat + dlat / 2),
                    (clon - dlon / 2, clat + dlat / 2),
                ]
            )
        )
        recs.append(
            {
                "zoning_code": int(zcode[k]),
                "zoning_load": ZONING_LOAD[int(zcode[k])],
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )
    return gpd.GeoDataFrame(recs, geometry=polys, crs=CRS_GEOGRAPHIC)


def noise_points(rng: np.random.Generator, per_km: int = 6) -> gpd.GeoDataFrame:
    """模擬 騒音測定点。幹線道路軸に沿って配置し LAeq(dB) を与える。

    実際の要請限度測定も幹線道路沿いにしか点が無く、
    「点しかないものを面にする」という本作の技術的主張の検証にちょうどよい。
    """
    rows = []
    for road, pts in _ARTERIALS:
        arr = np.array(pts)
        for i in range(len(arr) - 1):
            a, b = arr[i], arr[i + 1]
            seg_m = np.hypot(
                (b[0] - a[0]) * _M_PER_DEG_LON, (b[1] - a[1]) * _M_PER_DEG_LAT
            )
            n = max(2, int(seg_m / 1000.0 * per_km))
            for t in np.linspace(0, 1, n, endpoint=False):
                lon, lat = a + (b - a) * t
                # 幹線道路から少しずらした位置で測る（実際の測定局配置に倣う）
                lon += rng.normal(0, 0.0006)
                lat += rng.normal(0, 0.0006)
                u = float(_urbanity(np.array([lon]), np.array([lat]))[0])
                laeq = 62.0 + 12.0 * u + rng.normal(0, 1.8)
                rows.append(
                    {
                        "name": f"模擬測定点 {road}",
                        "road": road,
                        "laeq_db": round(float(laeq), 1),
                        "lon": float(lon),
                        "lat": float(lat),
                        "source": SYNTHETIC_SOURCE,
                        "synthetic": True,
                    }
                )

    # 幹線から離れた住宅地の背景測定点。これが無いと IDW が
    # 幹線の値を全域へ引き伸ばしてしまう。
    lon, lat = _sample_points(rng, 60, urban_bias=0.2)
    u = _urbanity(lon, lat)
    for i in range(len(lon)):
        rows.append(
            {
                "name": f"模擬測定点 一般地域 {i:02d}",
                "road": "",
                "laeq_db": round(float(48.0 + 14.0 * u[i] + rng.normal(0, 2.0)), 1),
                "lon": float(lon[i]),
                "lat": float(lat[i]),
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )
    return _gdf(pd.DataFrame(rows))


def parks() -> gpd.GeoDataFrame:
    """模擬 都市公園。位置は実在、形状は半径指定の円で近似。"""
    polys, recs = [], []
    for name, lon, lat, radius_m in _PARKS:
        dlon, dlat = _meters_to_deg(radius_m, radius_m)
        theta = np.linspace(0, 2 * np.pi, 48)
        polys.append(
            Polygon(list(zip(lon + dlon * np.cos(theta), lat + dlat * np.sin(theta))))
        )
        recs.append(
            {
                "name": f"{name}（模擬形状）",
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )
    return gpd.GeoDataFrame(recs, geometry=polys, crs=CRS_GEOGRAPHIC)


def daytime_population(
    rng: np.random.Generator, mesh_codes: list[str]
) -> pd.DataFrame:
    """模擬 昼間人口。メッシュコード直結合のテスト用に 3 次メッシュで返す。

    実際の e-Stat 地域メッシュ統計も 1km（3次）が基本粒度であり、
    250m メッシュへ按分する経路（aggregate.join_by_mesh_code）を通せる。
    """
    from . import mesh as meshlib

    codes3 = sorted({c[:8] for c in mesh_codes})
    rows = []
    for code in codes3:
        cell = meshlib.decode(code)
        clon, clat = cell.center
        u = float(_urbanity(np.array([clon]), np.array([clat]))[0])
        pop = 900 + u**1.6 * 46_000 * rng.uniform(0.8, 1.2)
        rows.append(
            {
                "mesh_code": code,
                "daytime_population": round(float(pop)),
                "source": SYNTHETIC_SOURCE,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 供給側
# ---------------------------------------------------------------------------


def host_facilities(rng: np.random.Generator, n: int = 85) -> gpd.GeoDataFrame:
    """模擬 公共施設（提言の割当先）。

    実在の図書館名を騙ると提言が本物に見えてしまうため、
    施設名はすべて「模擬」を冠した連番にしてある。
    実データ投入時にここが実名へ置き換わり、提言が成立する。
    """
    kinds = ["図書館", "区民センター", "出張所", "文化施設", "児童館", "公園管理施設"]
    probs = np.array([0.16, 0.22, 0.18, 0.10, 0.26, 0.08])
    lon, lat = _sample_points(rng, n, urban_bias=0.35)
    chosen = rng.choice(kinds, size=n, p=probs)

    rows = []
    counters: dict[str, int] = {}
    for i in range(n):
        k = str(chosen[i])
        counters[k] = counters.get(k, 0) + 1
        rows.append(
            {
                "name": f"模擬{k} #{counters[k]:02d}",
                "host_kind": k,
                "ward": "",
                "lon": float(lon[i]),
                "lat": float(lat[i]),
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )

    # 駅は既存アンカーからそのまま供給側候補にもする。
    for name, slon, slat, intensity in _CENTERS:
        if intensity < 0.3:
            continue
        rows.append(
            {
                "name": f"{name}（模擬・駅施設）",
                "host_kind": "駅",
                "ward": "",
                "lon": slon,
                "lat": slat,
                "source": SYNTHETIC_SOURCE,
                "synthetic": True,
            }
        )
    return _gdf(pd.DataFrame(rows))


def study_area() -> gpd.GeoDataFrame:
    """模擬 行政界。実データでは国土数値情報 N03 の区界に置き換わる。

    ここでは研究領域の矩形をそのまま 1 ポリゴンとして返す。
    """
    minx, miny, maxx, maxy = STUDY_BBOX
    poly = Polygon(
        [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    )
    return gpd.GeoDataFrame(
        [{"ward": "対象地域（模擬・矩形）", "source": SYNTHETIC_SOURCE}],
        geometry=[poly],
        crs=CRS_GEOGRAPHIC,
    )


def generate_all(seed: int = 20260726) -> dict:
    """全レイヤーをまとめて生成する。build.py から呼ばれる。"""
    rng = np.random.default_rng(seed)
    return {
        "welfare": welfare_facilities(rng),
        "schools": special_schools(rng),
        "clinics": clinics(rng),
        "stations": stations(rng),
        "zoning": zoning(rng),
        "noise": noise_points(rng),
        "parks": parks(),
        "hosts": host_facilities(rng),
        "area": study_area(),
        "_rng": rng,
    }
