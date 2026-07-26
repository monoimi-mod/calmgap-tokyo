"""
実オープンデータの取得と正規化。

    python -m etl.fetch --check      # 各ソースへの到達性だけ確認する
    python -m etl.fetch              # data/raw へ取得し data/processed へ正規化
    python -m etl.build --live       # 正規化済みデータでパイプラインを回す

⚠️ 開発環境の egress ポリシーにより、本モジュールの取得処理は
   実際の配信サーバに対して未検証である。列名マッピング
   （下記 COLUMN_MAP）は各データの仕様書に基づく想定値であり、
   実ファイルを 1 度落とした時点で必ず突き合わせること。
   `--check` と `inspect_columns()` はそのための道具。

設計方針:
    取得（fetch）と正規化（normalize）を分ける。
    生ファイルは data/raw にそのまま残し、再取得なしで
    正規化ロジックだけ何度でも作り直せるようにする。
    オープンデータは配信が止まることがあり、
    手元の生ファイルが唯一の原本になる場面がある。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from .config import (
    CRS_GEOGRAPHIC,
    DATA_PROCESSED,
    DATA_RAW,
    SOURCES,
    STUDY_BBOX,
    TARGET_WARDS,
    Source,
)
from .schema import (
    PSYCH_CLINIC_KEYWORDS,
    SCHOOL_CLASS_SPECIAL_NEEDS,
    ZONING_LOAD,
    ZONING_LOAD_DEFAULT,
    host_type,
    normalize_welfare_type,
    welfare_weight,
)

USER_AGENT = "calmgap-tokyo/0.1 (open data research; contact via repository)"
TIMEOUT = 60


# ---------------------------------------------------------------------------
# 列名マッピング（実ファイルで要検証）
# ---------------------------------------------------------------------------

# 各データの仕様書上の列名。実ファイルとズレたらここだけ直せば通る。
# 候補を複数並べ、最初に見つかったものを使う（年度で列名が変わるため）。
COLUMN_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "ksj_p29_school": {
        "class_code": ("P29_004",),
        "name": ("P29_005", "P29_006"),
        "students": ("P29_009",),
    },
    "ksj_p14_welfare": {
        "kind": ("P14_006", "P14_005"),
        "name": ("P14_007", "P14_006"),
        "capacity": ("P14_009", "P14_008"),
    },
    "ksj_p04_medical": {
        "name": ("P04_002",),
        "departments": ("P04_003",),
    },
    "ksj_a29_youto": {
        "zoning_code": ("A29_005", "A29_004"),
    },
    "wamnet_jigyosho": {
        "name": ("事業所名称", "事業所名"),
        "kind": ("サービス種別", "サービスの種類"),
        "capacity": ("定員", "利用定員"),
        "address": ("事業所住所", "所在地", "事業所所在地"),
        "lat": ("緯度",),
        "lon": ("経度",),
    },
    "tokyo_road_noise": {
        "laeq_db": ("昼間等価騒音レベル", "LAeq昼間", "等価騒音レベル(昼間)"),
        "lat": ("緯度",),
        "lon": ("経度",),
        "name": ("測定地点", "地点名"),
    },
    "tokyo_public_facility": {
        "name": ("施設名", "名称", "施設名称"),
        "lat": ("緯度",),
        "lon": ("経度",),
        "ward": ("区市町村", "自治体名"),
    },
}


def pick_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """候補の中から実在する列名を返す。無ければ None。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def inspect_columns(path: Path, n: int = 5) -> None:
    """落としたファイルの実際の列名を表示する。

    COLUMN_MAP を実ファイルに合わせて直すための最初の一手。
    """
    if path.suffix.lower() in (".csv", ".txt"):
        df = read_csv_japanese(path, nrows=n)
    else:
        df = gpd.read_file(path, rows=n)
    print(f"--- {path.name} / {len(df.columns)} 列")
    for c in df.columns:
        print(f"    {c!r:<28} 例: {df[c].iloc[0] if len(df) else ''!r}")


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


def download(url: str, dest: Path, force: bool = False, **kwargs) -> Path:
    """URL を data/raw へ保存する。既にあれば再取得しない。

    オープンデータの配信サーバは細く、同じファイルを何度も叩くのは
    先方の負荷であり自分の待ち時間でもある。キャッシュを既定にする。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        print(f"[cache] {dest.name} ({dest.stat().st_size:,} B)")
        return dest

    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            with requests.get(
                url, headers=headers, timeout=TIMEOUT, stream=True, **kwargs
            ) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
                tmp.replace(dest)
            print(f"[get] {dest.name} ({dest.stat().st_size:,} B)")
            return dest
        except Exception as e:  # noqa: BLE001 — 到達性の問題はすべて再試行対象
            last_err = e
            wait = 2**attempt
            print(f"[retry {attempt + 1}/4] {url} — {e} — {wait}s 待機", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"取得失敗: {url}") from last_err


def unzip(path: Path, dest_dir: Path) -> Path:
    """国土数値情報の ZIP を展開し、展開先ディレクトリを返す。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest_dir)
    return dest_dir


def read_csv_japanese(path: Path, **kwargs) -> pd.DataFrame:
    """日本の官公庁 CSV を読む。文字コードは CP932 が多く UTF-8 も混在する。"""
    for enc in ("utf-8-sig", "cp932", "utf-8", "euc_jp"):
        try:
            return pd.read_csv(path, encoding=enc, **kwargs)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "unknown", b"", 0, 1, f"{path.name} の文字コードを判定できない"
    )


def check_reachability() -> int:
    """全ソースへ HEAD を投げて到達性を表示する。取得前の切り分け用。"""
    failed = 0
    for s in SOURCES.values():
        if not s.url:
            print(f"  --  {s.key:<24} (手作業収集: {s.label})")
            continue
        try:
            r = requests.head(
                s.url,
                headers={"User-Agent": USER_AGENT},
                timeout=20,
                allow_redirects=True,
            )
            print(f"  {r.status_code}  {s.key:<24} {s.url}")
            if r.status_code >= 400:
                failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {s.key:<24} {type(e).__name__}: {e}")
            failed += 1

    if failed:
        print(
            f"\n{failed} 件が到達不可。ネットワーク制限下では模擬データで開発を続けられる:"
            "\n    python -m etl.build"
        )
    return failed


# ---------------------------------------------------------------------------
# 正規化ヘルパ
# ---------------------------------------------------------------------------


def _to_points(df: pd.DataFrame, lon_col: str, lat_col: str) -> gpd.GeoDataFrame:
    df = df.dropna(subset=[lon_col, lat_col]).copy()
    df["lon"] = pd.to_numeric(df[lon_col], errors="coerce")
    df["lat"] = pd.to_numeric(df[lat_col], errors="coerce")
    df = df.dropna(subset=["lon", "lat"])
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=CRS_GEOGRAPHIC
    )


def clip_to_study_area(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """研究領域の矩形でざっくり絞る。全国データを扱うため最初に効かせる。"""
    minx, miny, maxx, maxy = STUDY_BBOX
    return gdf.cx[minx:maxx, miny:maxy].copy()


def geocode_missing(df: pd.DataFrame, address_col: str) -> pd.DataFrame:
    """住所しか持たないレコードへ座標を与える。**最後の手段**。

    まず COLUMN_MAP の緯度経度列を探すこと。WAM NET の事業所一覧は
    年度によって座標を収録しており、その場合ジオコーディングは
    API 制限・処理時間・精度・利用規約のすべてで不利になるだけで、
    既にある座標より良い結果には決してならない。
    座標つきの国土数値情報 P14 でも代替できる。
    本関数は、どちらも取れなかった差分の補完に限定する。

    実運用では以下のいずれかを選ぶ:
      - 国土地理院 地理院地図 Geocoding API（少量・低速）
      - 位置参照情報（街区レベル）をローカルに落として突合（推奨・オフライン）
    未実装のまま呼ばれた場合は、座標なしレコードを落として警告する。
    """
    print(
        f"[warn] ジオコーディング未実装のため座標なし {len(df):,} 件を除外した。"
        " 国土数値情報 P14（座標つき）で概ね代替できる。",
        file=sys.stderr,
    )
    return df.iloc[0:0]


# ---------------------------------------------------------------------------
# ソース別の正規化
# ---------------------------------------------------------------------------


def normalize_schools(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 P29 から特別支援学校を抽出する。"""
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    m = COLUMN_MAP["ksj_p29_school"]
    cls = pick_column(gdf, m["class_code"])
    if cls:
        gdf = gdf[
            pd.to_numeric(gdf[cls], errors="coerce") == SCHOOL_CLASS_SPECIAL_NEEDS
        ]
    name_col = pick_column(gdf, m["name"])
    stu_col = pick_column(gdf, m["students"])

    students = (
        pd.to_numeric(gdf[stu_col], errors="coerce").fillna(150.0)
        if stu_col
        else pd.Series(150.0, index=gdf.index)
    )
    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col] if name_col else "特別支援学校",
            "kind": "特別支援学校",
            "capacity": students,
            "weight": 1.0,
            "demand_value": students,
            "source": SOURCES["ksj_p29_school"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry.centroid,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y
    return out.reset_index(drop=True)


def normalize_welfare(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 P14 福祉施設から障害福祉サービス事業所を抽出する。"""
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    m = COLUMN_MAP["ksj_p14_welfare"]
    kind_col = pick_column(gdf, m["kind"])
    name_col = pick_column(gdf, m["name"])
    cap_col = pick_column(gdf, m["capacity"])

    kinds = gdf[kind_col].astype(str) if kind_col else pd.Series("", index=gdf.index)
    capacity = (
        pd.to_numeric(gdf[cap_col], errors="coerce").fillna(20.0)
        if cap_col
        else pd.Series(20.0, index=gdf.index)
    )
    weights = kinds.map(welfare_weight)

    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col] if name_col else "",
            "kind": kinds,
            "capacity": capacity,
            "weight": weights,
            "demand_value": capacity * weights,
            "source": SOURCES["ksj_p14_welfare"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry.centroid,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y
    return out.reset_index(drop=True)


def normalize_wamnet(path: Path, geocode: bool = False) -> gpd.GeoDataFrame:
    """WAM NET 障害福祉サービス等事業所一覧を点データにする。

    **座標列があればジオコーディングしない。** 年度によって緯度経度が
    収録されている。住所から引き直すのは API 制限・処理時間・精度・
    利用規約のすべてで不利であり、既にある座標より良くなることはない。

    座標が無い年度のときだけ geocode=True で補完経路に入る（既定は無効）。
    その場合も国土数値情報 P14（座標つき）で代替できないか先に検討すること。

    WAM NET を使う利点は P14 より **サービス種別と定員が細かい** こと。
    需要重み（schema.WELFARE_DEMAND_WEIGHT）は種別ごとに大きく違うため、
    種別が取れるかどうかが需要スコアの質を直接左右する。
    """
    df = read_csv_japanese(path)
    m = COLUMN_MAP["wamnet_jigyosho"]

    name_col = pick_column(df, m["name"])
    kind_col = pick_column(df, m["kind"])
    cap_col = pick_column(df, m["capacity"])
    lon_col = pick_column(df, m["lon"])
    lat_col = pick_column(df, m["lat"])

    if lon_col and lat_col:
        print(f"[wamnet] 座標列を検出（{lat_col}/{lon_col}）。ジオコーディングは行わない。")
        gdf = _to_points(df, lon_col, lat_col)
        dropped = len(df) - len(gdf)
        if dropped:
            print(f"[wamnet] 座標が空の {dropped:,} 件を除外")
    else:
        addr_col = pick_column(df, m["address"])
        if not (geocode and addr_col):
            raise ValueError(
                f"{path.name} に緯度経度列が無い。実列名を確認したうえで "
                "COLUMN_MAP['wamnet_jigyosho'] を修正するか、"
                "座標つきの国土数値情報 P14（normalize_welfare）を使うこと:\n"
                f"    python -m etl.fetch --inspect {path}"
            )
        df = geocode_missing(df, addr_col)
        gdf = _to_points(df, "lon", "lat")

    gdf = clip_to_study_area(gdf)

    kinds = (
        gdf[kind_col].astype(str).map(normalize_welfare_type)
        if kind_col
        else pd.Series("", index=gdf.index)
    )
    capacity = (
        pd.to_numeric(gdf[cap_col], errors="coerce").fillna(20.0)
        if cap_col
        else pd.Series(20.0, index=gdf.index)
    )
    weights = kinds.map(welfare_weight)

    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col] if name_col else "",
            "kind": kinds,
            "capacity": capacity,
            "weight": weights,
            "demand_value": capacity * weights,
            "source": SOURCES["wamnet_jigyosho"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y
    return out.reset_index(drop=True)


def normalize_clinics(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 P04 から精神科・心療内科を抽出する。"""
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    m = COLUMN_MAP["ksj_p04_medical"]
    dep_col = pick_column(gdf, m["departments"])
    name_col = pick_column(gdf, m["name"])

    if dep_col:
        pattern = "|".join(PSYCH_CLINIC_KEYWORDS)
        gdf = gdf[gdf[dep_col].astype(str).str.contains(pattern, na=False)]

    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col] if name_col else "",
            "kind": "精神科・心療内科",
            "capacity": 1.0,
            "weight": 1.0,
            "demand_value": 1.0,
            "source": SOURCES["ksj_p04_medical"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry.centroid,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y
    return out.reset_index(drop=True)


def normalize_zoning(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 A29 用途地域を負荷スコアつきポリゴンにする。"""
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    col = pick_column(gdf, COLUMN_MAP["ksj_a29_youto"]["zoning_code"])
    codes = (
        pd.to_numeric(gdf[col], errors="coerce")
        if col
        else pd.Series(float("nan"), index=gdf.index)
    )
    return gpd.GeoDataFrame(
        {
            "zoning_code": codes,
            "zoning_load": codes.map(ZONING_LOAD).fillna(ZONING_LOAD_DEFAULT),
            "source": SOURCES["ksj_a29_youto"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry,
        crs=CRS_GEOGRAPHIC,
    ).reset_index(drop=True)


def normalize_noise(path: Path) -> gpd.GeoDataFrame:
    """東京都環境局の騒音測定結果を点データにする。"""
    df = read_csv_japanese(path)
    m = COLUMN_MAP["tokyo_road_noise"]
    lon_col, lat_col = pick_column(df, m["lon"]), pick_column(df, m["lat"])
    if not (lon_col and lat_col):
        raise ValueError(
            f"{path.name} に緯度経度列が無い。実列名を確認: "
            f"python -c \"from etl.fetch import inspect_columns,Path;"
            f"inspect_columns(Path('{path}'))\""
        )
    laeq_col = pick_column(df, m["laeq_db"])
    name_col = pick_column(df, m["name"])

    gdf = clip_to_study_area(_to_points(df, lon_col, lat_col))
    gdf["laeq_db"] = (
        pd.to_numeric(gdf[laeq_col], errors="coerce")
        if laeq_col
        else float("nan")
    )
    gdf["name"] = gdf[name_col] if name_col else ""
    gdf["source"] = SOURCES["tokyo_road_noise"].label
    gdf["synthetic"] = False
    return gdf[
        ["name", "laeq_db", "lon", "lat", "source", "synthetic", "geometry"]
    ].reset_index(drop=True)


def normalize_hosts(path: Path) -> gpd.GeoDataFrame:
    """公共施設一覧をホスト施設候補にする。"""
    df = read_csv_japanese(path)
    m = COLUMN_MAP["tokyo_public_facility"]
    lon_col, lat_col = pick_column(df, m["lon"]), pick_column(df, m["lat"])
    name_col = pick_column(df, m["name"])
    ward_col = pick_column(df, m["ward"])

    gdf = clip_to_study_area(_to_points(df, lon_col, lat_col))
    gdf["name"] = gdf[name_col] if name_col else ""
    gdf["host_kind"] = gdf["name"].map(host_type)
    gdf["ward"] = gdf[ward_col] if ward_col else ""
    gdf["source"] = SOURCES["tokyo_public_facility"].label
    gdf["synthetic"] = False
    # ホスト種別を判定できない施設（学校・保育園等）は候補から外す。
    gdf = gdf[gdf["host_kind"].notna()]
    return gdf[
        ["name", "host_kind", "ward", "lon", "lat", "source", "synthetic", "geometry"]
    ].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 正規化済みデータの読み書き
# ---------------------------------------------------------------------------

# build.py が期待するレイヤー名 → data/processed のファイル名
PROCESSED_FILES: dict[str, str] = {
    "welfare": "welfare.geojson",
    "schools": "schools.geojson",
    "clinics": "clinics.geojson",
    "stations": "stations.geojson",
    "zoning": "zoning.geojson",
    "noise": "noise.geojson",
    "parks": "parks.geojson",
    "hosts": "hosts.geojson",
    "area": "area.geojson",
}


def save_processed(layers: dict) -> None:
    for key, filename in PROCESSED_FILES.items():
        gdf = layers.get(key)
        if gdf is None or not len(gdf):
            continue
        path = DATA_PROCESSED / filename
        gdf.to_file(path, driver="GeoJSON")
        print(f"[normalize] {filename} ({len(gdf):,} 件)")


def load_processed() -> dict:
    """data/processed から全レイヤーを読む。build.py --live の入口。"""
    layers: dict = {}
    missing: list[str] = []
    for key, filename in PROCESSED_FILES.items():
        path = DATA_PROCESSED / filename
        if path.exists():
            layers[key] = gpd.read_file(path)
        else:
            missing.append(filename)

    if missing:
        raise FileNotFoundError(
            "data/processed に未生成のレイヤーがある: "
            + ", ".join(missing)
            + "\n先に `python -m etl.fetch` を実行するか、"
            "模擬データで動かす場合は --live を外すこと。"
        )

    pop_path = DATA_PROCESSED / "population.csv"
    if pop_path.exists():
        layers["population"] = read_csv_japanese(pop_path)
    return layers


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="オープンデータの取得と正規化")
    ap.add_argument("--check", action="store_true", help="到達性の確認のみ")
    ap.add_argument("--force", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--inspect", type=Path, help="落としたファイルの列名を表示")
    args = ap.parse_args(argv)

    if args.inspect:
        inspect_columns(args.inspect)
        return 0

    if args.check:
        return 0 if check_reachability() == 0 else 1

    print(
        "各ソースの配布形態（ZIP の中身・年度別 URL）は年により変わる。\n"
        "まず --check で到達性を確かめ、生ファイルを data/raw へ置いたうえで\n"
        "--inspect で実列名を確認し、COLUMN_MAP を合わせてから正規化すること。\n"
    )
    if check_reachability() != 0:
        return 1

    print(
        "\n到達性 OK。個別データの URL は年度ごとに変わるため、\n"
        "config.SOURCES の各 url からファイル本体のリンクを確認して\n"
        "data/raw へ配置したのち、normalize_* を呼ぶこと。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
