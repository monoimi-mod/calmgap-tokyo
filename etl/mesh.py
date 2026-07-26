"""
JIS X 0410 標準地域メッシュの実装。

e-Stat の地域メッシュ統計はメッシュコードで直接結合できるため、
独自グリッドではなく標準メッシュを採用する。これにより
昼間人口・国勢調査データが空間補間なしで結合できる。

対応次数:
    1次(80km) 4桁 / 2次(10km) 6桁 / 3次(1km) 8桁
    4次(500m) 9桁 / 5次(250m) 10桁

外部ライブラリに依存しない純粋な計算のみで構成する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

# 各次数のセルサイズ（度）。緯度方向・経度方向。
# 1次 = 緯度40分 × 経度1度 を起点に、2次で1/8、3次で1/10、以降1/2ずつ。
_LAT_UNIT_1 = 40.0 / 60.0  # 40分
_LON_UNIT_1 = 1.0

CELL_SIZE: dict[int, tuple[float, float]] = {
    1: (_LAT_UNIT_1, _LON_UNIT_1),
    2: (_LAT_UNIT_1 / 8, _LON_UNIT_1 / 8),
    3: (_LAT_UNIT_1 / 8 / 10, _LON_UNIT_1 / 8 / 10),
    4: (_LAT_UNIT_1 / 8 / 10 / 2, _LON_UNIT_1 / 8 / 10 / 2),
    5: (_LAT_UNIT_1 / 8 / 10 / 4, _LON_UNIT_1 / 8 / 10 / 4),
}

CODE_LENGTH: dict[int, int] = {1: 4, 2: 6, 3: 8, 4: 9, 5: 10}

# 各次数の呼称（UI・ドキュメント表示用）
LEVEL_LABEL: dict[int, str] = {
    1: "1次メッシュ(約80km)",
    2: "2次メッシュ(約10km)",
    3: "3次メッシュ(約1km)",
    4: "4次メッシュ(約500m)",
    5: "5次メッシュ(約250m)",
}


@dataclass(frozen=True)
class MeshCell:
    """1 メッシュ。code と南西端・北東端の緯度経度を持つ。"""

    code: str
    level: int
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @property
    def center(self) -> tuple[float, float]:
        """(lon, lat) 重心。GeoJSON の座標順に合わせる。"""
        return (
            (self.min_lon + self.max_lon) / 2,
            (self.min_lat + self.max_lat) / 2,
        )

    def polygon_coords(self) -> list[list[float]]:
        """GeoJSON Polygon の外環リング（反時計回り・始点で閉じる）。"""
        return [
            [self.min_lon, self.min_lat],
            [self.max_lon, self.min_lat],
            [self.max_lon, self.max_lat],
            [self.min_lon, self.max_lat],
            [self.min_lon, self.min_lat],
        ]


def encode(lat: float, lon: float, level: int = 5) -> str:
    """緯度経度から地域メッシュコードを求める。

    >>> encode(35.6580, 139.7016, 3)   # 渋谷駅付近
    '53393586'
    >>> encode(35.6812, 139.7671, 3)   # 東京駅付近
    '53394611'
    """
    if level not in CELL_SIZE:
        raise ValueError(f"未対応のメッシュ次数: {level}")

    # --- 1次メッシュ ---
    # 緯度は 40分 単位、経度は 1度 単位（100度を原点とする）。
    p, rem_lat_min = divmod(lat * 60.0, 40.0)  # rem は分
    u = int(lon) - 100
    rem_lon_deg = lon - int(lon)
    code = f"{int(p):02d}{u:02d}"
    if level == 1:
        return code

    # --- 2次メッシュ --- 1次を 8×8 に分割（緯度5分 / 経度7.5分）
    q, rem_lat_min = divmod(rem_lat_min, 5.0)
    v, rem_lon_min = divmod(rem_lon_deg * 60.0, 7.5)
    code += f"{int(q)}{int(v)}"
    if level == 2:
        return code

    # --- 3次メッシュ --- 2次を 10×10 に分割（緯度30秒 / 経度45秒）
    r, rem_lat_sec = divmod(rem_lat_min * 60.0, 30.0)
    w, rem_lon_sec = divmod(rem_lon_min * 60.0, 45.0)
    code += f"{int(r)}{int(w)}"
    if level == 3:
        return code

    # --- 4次メッシュ(500m) --- 3次を 2×2 に分割
    # 象限番号は 1=南西 2=南東 3=北西 4=北東。
    s_lat, rem_lat_sec = divmod(rem_lat_sec, 15.0)
    s_lon, rem_lon_sec = divmod(rem_lon_sec, 22.5)
    code += f"{int(s_lat) * 2 + int(s_lon) + 1}"
    if level == 4:
        return code

    # --- 5次メッシュ(250m) --- 4次をさらに 2×2 に分割
    t_lat = int(rem_lat_sec // 7.5)
    t_lon = int(rem_lon_sec // 11.25)
    code += f"{t_lat * 2 + t_lon + 1}"
    return code


def decode(code: str) -> MeshCell:
    """メッシュコードから MeshCell（南西端と大きさ）を復元する。"""
    code = code.strip()
    level = next((lv for lv, n in CODE_LENGTH.items() if n == len(code)), None)
    if level is None:
        raise ValueError(f"メッシュコードの桁数が不正: {code!r}")

    lat = int(code[0:2]) * 40.0 / 60.0
    lon = int(code[2:4]) + 100.0

    if level >= 2:
        lat += int(code[4]) * 5.0 / 60.0
        lon += int(code[5]) * 7.5 / 60.0
    if level >= 3:
        lat += int(code[6]) * 30.0 / 3600.0
        lon += int(code[7]) * 45.0 / 3600.0
    if level >= 4:
        quad = int(code[8]) - 1
        lat += (quad // 2) * 15.0 / 3600.0
        lon += (quad % 2) * 22.5 / 3600.0
    if level >= 5:
        quad = int(code[9]) - 1
        lat += (quad // 2) * 7.5 / 3600.0
        lon += (quad % 2) * 11.25 / 3600.0

    dlat, dlon = CELL_SIZE[level]
    return MeshCell(
        code=code,
        level=level,
        min_lat=lat,
        min_lon=lon,
        max_lat=lat + dlat,
        max_lon=lon + dlon,
    )


def parent(code: str, level: int) -> str:
    """上位次数のメッシュコードを切り出す（e-Stat の粗いデータとの結合用）。"""
    n = CODE_LENGTH[level]
    if len(code) < n:
        raise ValueError(f"{code!r} は {level} 次メッシュより粗い")
    return code[:n]


def iter_bbox(
    bbox: tuple[float, float, float, float], level: int = 5
) -> Iterator[MeshCell]:
    """矩形範囲を覆うメッシュを列挙する。

    bbox は (minx, miny, maxx, maxy) = (min_lon, min_lat, max_lon, max_lat)。
    浮動小数の丸め誤差でセルを取りこぼさないよう、南西端から
    セルサイズ刻みで進めるのではなく、整数インデックスで走査する。
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    dlat, dlon = CELL_SIZE[level]

    # 南西端セルの原点に吸着させ、そこからの整数ステップで列挙する。
    sw = decode(encode(min_lat, min_lon, level))
    n_lat = int((max_lat - sw.min_lat) / dlat) + 1
    n_lon = int((max_lon - sw.min_lon) / dlon) + 1

    for i in range(n_lat):
        lat = sw.min_lat + i * dlat
        for j in range(n_lon):
            lon = sw.min_lon + j * dlon
            # セル中心でコード化すると境界の丸め誤差を避けられる。
            yield decode(encode(lat + dlat / 2, lon + dlon / 2, level))


def cell_size_meters(level: int, lat: float = 35.66) -> tuple[float, float]:
    """指定緯度における概算セル寸法 (南北 m, 東西 m)。ログ表示用。"""
    import math

    dlat, dlon = CELL_SIZE[level]
    return (dlat * 111_320.0, dlon * 111_320.0 * math.cos(math.radians(lat)))
