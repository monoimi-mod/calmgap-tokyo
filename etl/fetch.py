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
import inspect
import io
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from .config import (
    CLIP_BUFFER_M,
    CRS_GEOGRAPHIC,
    DATA_PROCESSED,
    DATA_RAW,
    SOURCES,
    STUDY_BBOX,
    TARGET_WARDS,
    TARGET_WARD_CODES,
    Source,
)
from .schema import (
    ZONING_NAME,
    P14_HOST_SUBCLASS,
    P14_SUBCLASS,
    P14_SUBCLASS_DEFAULT_SCOPE,
    is_disability_name,
    assumed_capacity,
    classify_welfare_by_name,
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
    # 実データ P14-21_13（東京都・2022-03-11 版）で確認済み。
    #   P14_001 都道府県名 / P14_002 市区町村名 / P14_003 行政区域コード
    #   P14_004 所在地     / P14_005 大分類     / P14_006 中分類
    #   P14_007 小分類     / P14_008 名称       / P14_009 設置主体コード
    #   P14_010 位置正確度コード
    # 定員フィールドは存在しない（schema.P14_SUBCLASS のコメント参照）。
    "ksj_p14_welfare": {
        "pref": ("P14_001",),
        "city": ("P14_002",),
        "address": ("P14_004",),
        "major": ("P14_005",),
        "subclass": ("P14_007",),
        "name": ("P14_008",),
    },
    "ksj_p04_medical": {
        "name": ("P04_002",),
        "departments": ("P04_003",),
    },
    # 実データ A29-19_13（東京都・2019年版）で確認済み。
    # 用途地域コードは **A29_004**。当初 A29_005 と推測していたが外れており、
    # 全件 NaN のまま既定値 0.30 が乗って静かに壊れていた。
    # 現在は _extract_zoning_codes が全列を走査して自動判定するので、
    # ここは「確認済みの列を先頭に置く」記録としての意味を持つ。
    "ksj_a29_youto": {
        "zoning_code": ("A29_004", "A29_005"),
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


# ---------------------------------------------------------------------------
# 列の特定（決め打ちが外れたときに黙って通さないための仕掛け）
# ---------------------------------------------------------------------------

# A29 用途地域で実際に起きた事故:
#   仕様書からの推測で列名を 1 つ決め打ちし、それが外れていた。
#   `pick_column` が None を返し、呼び出し側が `if col:` で分岐して
#   既定値を全件に乗せたため、**ビルドもテストも成功と表示されたまま
#   負荷スコアの主軸が消えていた**。
#
# 同じ構造は列名を決め打ちする全レイヤーにある。そこで:
#
#   1. 列名で見つからなければ、列の **中身** から探す（detect_column）
#   2. それでも見つからなければ、実際の値を添えて例外で止める（require_column）
#   3. 数値が変わらない飾りの列だけ、無くても進む（optional_column）
#
# 「取りこぼしても混入させない」と同じ判断。既定値を静かに乗せるより、
# 止まって列名を教えてもらう方が安い。


def _column_samples(df: pd.DataFrame, n: int = 8) -> list[str]:
    """各列の実際の値を数個ずつ並べる。エラーメッセージに添えるため。"""
    lines = []
    for col in df.columns:
        if col == "geometry":
            continue
        vals = df[col].dropna().unique()[:n]
        lines.append(f"    {col}: {list(vals)}")
    return lines


def detect_column(
    df: pd.DataFrame,
    predicate,
    *,
    tag: str,
    label: str,
    min_ratio: float = 0.9,
    whole: object = None,
    name_hint: str | None = None,
    require_hint: bool = False,
) -> str | None:
    """列の中身から目的の列を探す。

    predicate は列（Series）を受け取り、真偽の Series を返す。
    その真率が min_ratio 以上の列を候補とする。

    whole は列全体に対する条件（値の種類数など）。用途地域コードで
    「東京都なら全件 13 の都道府県コードが混じる」問題に当たったように、
    1 セルずつの判定では区別できない列がある。

    name_hint は同じ形の列が複数あるときの優先指定（正規表現）。
    騒音の昼間 dB と夜間 dB のように、値域では区別できない場合に使う。

    require_hint=True にすると name_hint に合う列しか候補にしない。
    「ただの数値」から意味を当てられない列に使う——たとえば児童生徒数と
    建築年は値域が重なるため、中身だけで見分けようとすると
    **間違った列を掴んで平然と通る**。列名の手掛かりが無いなら諦めて止まる方がよい。
    """
    hits: list[tuple[str, float, bool]] = []
    for col in df.columns:
        if col == "geometry":
            continue
        if require_hint and not (name_hint and re.search(name_hint, str(col))):
            continue
        try:
            ratio = float(predicate(df[col]).mean())
        except Exception:  # noqa: BLE001 — 判定できない列は候補外
            continue
        if ratio < min_ratio:
            continue
        if whole is not None and not whole(df[col]):
            continue
        preferred = bool(name_hint and re.search(name_hint, str(col)))
        hits.append((col, ratio, preferred))

    if not hits:
        return None
    # 優先指定に合うものを先に、次に真率の高いものを選ぶ。
    col, ratio, _ = max(hits, key=lambda h: (h[2], h[1]))
    print(f"[{tag}] {label}の列を中身から特定: {col}（該当 {ratio * 100:.0f}%）")
    if len(hits) > 1:
        others = ", ".join(c for c, _, _ in hits if c != col)
        print(f"[{tag}] 　同じ条件に合う列が他にもある: {others}", file=sys.stderr)
    return col


def require_column(
    df: pd.DataFrame,
    path: Path,
    kind: str,
    field: str,
    *,
    tag: str,
    label: str,
    why: str,
    predicate=None,
    min_ratio: float = 0.9,
    whole: object = None,
    name_hint: str | None = None,
    require_hint: bool = False,
) -> str:
    """数値に影響する列を必ず特定する。できなければ実値を添えて止まる。"""
    candidates = COLUMN_MAP[kind][field]
    col = pick_column(df, candidates)
    if col is not None:
        return col
    if predicate is not None:
        col = detect_column(
            df,
            predicate,
            tag=tag,
            label=label,
            min_ratio=min_ratio,
            whole=whole,
            name_hint=name_hint,
            require_hint=require_hint,
        )
        if col is not None:
            return col
    raise ValueError(
        "\n".join(
            [
                f"{path.name} から{label}の列を特定できない。",
                why,
                "",
                f"探した列名: {', '.join(candidates)}",
                "中身からの推定も一致しなかった。",
                "",
                "各列の実際の値:",
                *_column_samples(df),
                "",
                f"{label}の列を見つけて "
                f"etl/fetch.py の COLUMN_MAP[{kind!r}][{field!r}] に追加すること。",
                f"詳細: python -m etl.fetch --inspect {path}",
            ]
        )
    )


def optional_column(
    df: pd.DataFrame, kind: str, field: str, *, tag: str, label: str, fallback: str
) -> str | None:
    """無くても数値が変わらない列。欠けたことは必ず告げる。"""
    col = pick_column(df, COLUMN_MAP[kind][field])
    if col is None:
        print(f"[{tag}] {label}の列が無い（{fallback}）", file=sys.stderr)
    return col


# 値域から列を推定するための定数。
# 「東京都のデータである」ことを前提に、緯度経度が入り得る幅を広く取る。
TOKYO_LON_RANGE = (138.9, 140.0)
TOKYO_LAT_RANGE = (34.9, 36.2)
# 等価騒音レベル LAeq の現実的な幅。要請限度は昼間 65〜75dB 前後。
NOISE_DB_RANGE = (30.0, 110.0)
# 列が特定できないときの既定値（--assume-missing でのみ使う）。
SCHOOL_STUDENTS_FALLBACK = 150.0
WAMNET_CAPACITY_FALLBACK = 20.0


def require_nonempty(n: int, *, tag: str, what: str, why: str) -> None:
    """絞り込みの結果が 0 件なら止める。

    列を取り違えると「条件に合う行が 1 件も無い」形で現れることがある。
    0 件のレイヤーは、実データとして数えられたまま構成要素を消してしまう。
    """
    if n == 0:
        raise ValueError(f"[{tag}] {what}が 0 件。{why}")


# 中身を見る価値のある拡張子。SHP は .shx/.dbf/.prj を伴うが、
# それらは .shp の付属ファイルなので単独では開かない。
INSPECTABLE_SUFFIXES = (".shp", ".geojson", ".json", ".csv", ".txt", ".gml")


def inspect_columns(path: Path, n: int = 5) -> None:
    """落としたファイルの実際の列名を表示する。

    COLUMN_MAP を実ファイルに合わせて直すための最初の一手。
    ディレクトリを渡すと、その下にあるデータファイルを再帰的に列挙して
    それぞれの件数と列名を出す。国土数値情報は年度やデータ種別によって
    「1 県 1 ファイル」だったり「市区町村ごとに分割」だったりするため、
    まず何が入っているかを一望できないと手の付けようがない。
    """
    if not path.exists():
        # 存在しないパスを GDAL へ渡すと DataSourceError になって
        # 「名前が違う」のか「壊れている」のか分からない。ここで切り分ける。
        print(f"{path} が存在しない。")
        parent = path.parent
        if parent.exists():
            stem = path.name.lower()[:3]
            near = sorted(
                p.name for p in parent.iterdir() if stem and stem in p.name.lower()
            )
            listing = near or sorted(p.name for p in parent.iterdir())[:40]
            label = "似た名前" if near else f"{parent} の中身（先頭40件）"
            print(f"\n{label}:")
            for name in listing:
                print(f"    {name}")
            if not near:
                print("\nZIP のままなら先に展開すること。")
        else:
            print(f"親ディレクトリ {parent} も存在しない。")
        return

    if path.is_dir():
        files = sorted(
            p
            for p in path.rglob("*")
            if p.is_file() and p.suffix.lower() in INSPECTABLE_SUFFIXES
        )
        if not files:
            print(f"{path} にデータファイルが見つからない。")
            print("ZIP のままなら先に展開すること。")
            return

        print(f"=== {path} 配下のデータファイル {len(files)} 件 ===\n")
        for p in files:
            size = p.stat().st_size
            try:
                if p.suffix.lower() in (".csv", ".txt"):
                    df = read_csv_japanese(p, nrows=n)
                    total = "?"
                else:
                    df = gpd.read_file(p, rows=n)
                    total = f"{len(gpd.read_file(p, columns=[])):,}"
                print(
                    f"  {p.relative_to(path)}  ({size:,} B / {total} 件 / "
                    f"{len(df.columns)} 列)"
                )
                print(f"      列: {list(df.columns)}")
            except Exception as e:  # noqa: BLE001 — 読めないファイルも一覧には出す
                print(f"  {p.relative_to(path)}  ({size:,} B) 読めない: {e}")
            print()
        return

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


def read_vector(path: Path) -> gpd.GeoDataFrame:
    """ファイル 1 本、またはディレクトリ配下の全ファイルを読んで結合する。

    国土数値情報は種別・年度によって配布単位が変わる。
    N03 行政区域は「1 県 1 ファイル」だが、A29 用途地域のように
    **都市計画区域ごとに数十ファイルへ分割**されているものもある。
    利用者にどれが本体かを判断させるのは筋が悪いので、
    フォルダをそのまま渡せるようにして、こちらで結合する。

    同じデータが .shp と .geojson の両方で入っていることがあるため、
    GeoJSON があればそちらを優先する（Shift-JIS の DBF を避けられる）。
    """
    if path.is_file():
        return gpd.read_file(path)

    if not path.is_dir():
        raise FileNotFoundError(f"{path} が存在しない")

    geojson = sorted(p for p in path.rglob("*") if p.suffix.lower() in (".geojson",))
    shp = sorted(p for p in path.rglob("*") if p.suffix.lower() == ".shp")
    files = geojson or shp
    if not files:
        raise FileNotFoundError(
            f"{path} に .shp / .geojson が無い。ZIP のままなら先に展開すること。"
        )

    fmt = "GeoJSON" if geojson else "SHP"

    # 【罠】県全体ファイルと市区町村別ファイルが同梱されていることがある。
    #
    # A29 用途地域（東京都）は
    #     A29-19_13000  … 都全域 10,684 件
    #     A29-19_13101  … 千代田区 92 件
    #     A29-19_13112  … 世田谷区 520 件   …（以下 47 市区町村）
    # が同じフォルダに入っている。全部読むと同じポリゴンを 2 回数えることになり、
    # 面積加重平均が壊れる（同じ場所に用途地域が二重に乗る）。
    #
    # 末尾が「都道府県コード + 000」のファイルは全域版なので、
    # それがあればそれだけを使う。
    aggregate = [p for p in files if re.search(r"_\d{2}000(?:[._]|$)", p.stem)]
    if aggregate and len(files) > len(aggregate):
        skipped = len(files) - len(aggregate)
        print(
            f"[read] 全域ファイル {aggregate[0].name} を検出。"
            f"市区町村別 {skipped} ファイルは同じ内容の内訳なので読まない（二重計上防止）"
        )
        files = aggregate[:1]

    print(f"[read] {path} から {fmt} を {len(files)} ファイル読み込む")

    frames = []
    for p in files:
        try:
            g = gpd.read_file(p)
        except UnicodeDecodeError:
            g = gpd.read_file(p, encoding="cp932")
        if len(g):
            frames.append(g)

    if not frames:
        raise ValueError(f"{path} 配下のファイルがすべて空だった")

    # 分割ファイルは列構成が揃っている前提だが、年度混在に備えて和集合で結合する。
    merged = pd.concat(frames, ignore_index=True)
    out = gpd.GeoDataFrame(merged, geometry="geometry", crs=frames[0].crs)
    if len(frames) > 1:
        print(f"[read] 結合後 {len(out):,} 件 / {len(out.columns)} 列")
    return out


def _to_points(df: pd.DataFrame, lon_col: str, lat_col: str) -> gpd.GeoDataFrame:
    df = df.dropna(subset=[lon_col, lat_col]).copy()
    df["lon"] = pd.to_numeric(df[lon_col], errors="coerce")
    df["lat"] = pd.to_numeric(df[lat_col], errors="coerce")
    df = df.dropna(subset=["lon", "lat"])
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=CRS_GEOGRAPHIC
    )


def area_path() -> Path:
    return DATA_PROCESSED / PROCESSED_FILES["area"]


def study_bbox(buffer_m: float = CLIP_BUFFER_M) -> tuple[float, float, float, float]:
    """研究領域の矩形を返す。

    行政界（data/processed/area.geojson）があればそこから導出し、
    無ければ config.STUDY_BBOX の暫定値を使う。

    buffer_m は区界の外側に取る余白。境界のすぐ外にある事業所や駅を
    落とすと縁のメッシュの需要が不自然に低く出るため
    （config.CLIP_BUFFER_M のコメント参照）、入力データの絞り込みには
    余白付きの矩形を使う。メッシュ自体は build 側で区界ポリゴンにより厳密に切る。
    """
    p = area_path()
    if not p.exists():
        return STUDY_BBOX

    area = gpd.read_file(p).to_crs(CRS_GEOGRAPHIC)
    minx, miny, maxx, maxy = area.total_bounds
    if buffer_m <= 0:
        return (float(minx), float(miny), float(maxx), float(maxy))

    # 緯度経度への換算。東京付近の 1 度あたりの距離で割る。
    import math

    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * math.cos(math.radians((miny + maxy) / 2)))
    return (
        float(minx - dlon),
        float(miny - dlat),
        float(maxx + dlon),
        float(maxy + dlat),
    )


def clip_to_study_area(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """研究領域の矩形でざっくり絞る。全国データを扱うため最初に効かせる。"""
    minx, miny, maxx, maxy = study_bbox()
    return gdf.cx[minx:maxx, miny:maxy].copy()


def normalize_area(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 N03 から対象区の行政界を抽出する。

    これが入るまで研究領域は暫定の矩形であり、対象2区の外
    （港区・目黒区・品川区の一部）まで含んでしまっていた。
    「渋谷区と世田谷区が対象」と言いながら港区の施設を提言するのは
    提言物として成立しないため、優先して投入すべきレイヤー。

    N03 の SHP は DBF が Shift-JIS で、環境によって名称が化ける。
    そのため名称と行政区域コードの両方で判定する。
    """
    gdf = gpd.read_file(path)
    # SHP を UTF-8 として読むと名称が化けるので、化けていたら読み直す。
    if "N03_004" in gdf.columns and not gdf["N03_004"].astype(str).str.contains(
        "区|市|町|村", na=False
    ).any():
        print("[n03] 名称が化けているため cp932 で読み直す")
        gdf = gpd.read_file(path, encoding="cp932")

    gdf = gdf.to_crs(CRS_GEOGRAPHIC)
    name_col = "N03_004" if "N03_004" in gdf.columns else None
    code_col = "N03_007" if "N03_007" in gdf.columns else None
    if not (name_col or code_col):
        raise ValueError(
            f"{path.name} に市区町村名/行政区域コード列が無い:\n"
            f"    python -m etl.fetch --inspect {path}"
        )

    mask = pd.Series(False, index=gdf.index)
    if name_col:
        mask |= gdf[name_col].astype(str).isin(TARGET_WARDS)
    if code_col:
        mask |= gdf[code_col].astype(str).isin(TARGET_WARD_CODES)

    sel = gdf[mask].copy()
    if len(sel) == 0:
        raise ValueError(
            f"対象区が見つからない。config.TARGET_WARDS={TARGET_WARDS} / "
            f"TARGET_WARD_CODES={TARGET_WARD_CODES} を確認すること。"
        )

    # 1 区が複数ポリゴンに分かれている場合があるので区単位に融合する。
    key = name_col or code_col
    sel["ward"] = sel[key].astype(str)
    dissolved = sel[["ward", "geometry"]].dissolve(by="ward", as_index=False)
    dissolved["source"] = "国土数値情報 N03 行政区域"
    dissolved["synthetic"] = False

    for _, row in dissolved.iterrows():
        b = row.geometry.bounds
        print(f"[n03] {row['ward']}: bounds=({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})")
    minx, miny, maxx, maxy = dissolved.total_bounds
    print(f"[n03] 研究領域 = ({minx:.4f}, {miny:.4f}, {maxx:.4f}, {maxy:.4f})")

    return dissolved.reset_index(drop=True)


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


def normalize_schools(path: Path, assume_missing: bool = False) -> gpd.GeoDataFrame:
    """国土数値情報 P29 から特別支援学校を抽出する。

    学校分類コードの列を取り違えると、**全学校が特別支援学校として**
    需要に乗る（小中高を含めれば件数は数十倍になる）。
    生徒数の列を取り違えると、全件が既定値 150 人になり
    「規模で重み付けする」という前提そのものが消える。
    どちらも黙って通さない。
    """
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    total = len(gdf)
    require_nonempty(
        total, tag="p29", what="研究領域内の学校", why="対象範囲か入力を確認すること。"
    )

    cls = require_column(
        gdf,
        path,
        "ksj_p29_school",
        "class_code",
        tag="p29",
        label="学校分類コード",
        why=(
            "この列で特別支援学校に絞る。特定できないまま進めると"
            "小中高を含む全学校が需要に乗る。"
        ),
        predicate=lambda s: pd.to_numeric(s, errors="coerce").notna(),
        whole=lambda s: (
            SCHOOL_CLASS_SPECIAL_NEEDS
            in set(pd.to_numeric(s, errors="coerce").dropna().astype(int))
            and 2 <= int(s.nunique()) <= 30
        ),
        name_hint=r"class|種別|分類",
    )
    codes = pd.to_numeric(gdf[cls], errors="coerce")
    gdf = gdf[codes == SCHOOL_CLASS_SPECIAL_NEEDS]
    if len(gdf) == 0:
        breakdown = ", ".join(
            f"{int(c)}: {n}件" for c, n in codes.value_counts().head(20).items()
        )
        raise ValueError(
            f"{cls} に特別支援学校のコード {SCHOOL_CLASS_SPECIAL_NEEDS} が"
            f"1 件も無い（研究領域内 {total:,}件）。\n"
            f"    {cls} の実際の内訳: {breakdown}\n"
            "年度によってコード体系が違う可能性がある（16001 等の桁数違いを含む）。"
            "schema.SCHOOL_CLASS_SPECIAL_NEEDS を実データに合わせること。"
        )
    print(f"[p29] {total:,}件 → 特別支援学校 {len(gdf):,}件（{cls} で判定）")

    name_col = optional_column(
        gdf,
        "ksj_p29_school",
        "name",
        tag="p29",
        label="学校名",
        fallback="根拠カードの表示名が『特別支援学校』になる",
    )
    stu_col = pick_column(gdf, COLUMN_MAP["ksj_p29_school"]["students"])
    if stu_col is None and not assume_missing:
        stu_col = require_column(
            gdf,
            path,
            "ksj_p29_school",
            "students",
            tag="p29",
            label="児童生徒数",
            why=(
                "需要は学校の規模で重み付けする。特定できないまま進めると"
                f"全件が既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人になり、"
                "大規模校と小規模校が同じ重さになる。\n"
                "本当に収録されていない年度なら --assume-missing を付けて"
                "既定値を使う（過小・過大の両方を含むと明示される）。"
            ),
            # 児童生徒数は「ただの正の整数」で、建築年や座標系コードと
            # 値域が重なる。列名の手掛かりが無ければ推定しない。
            predicate=lambda s: pd.to_numeric(s, errors="coerce").between(1, 5000),
            whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 5,
            name_hint=r"児童|生徒|人数|在籍",
            require_hint=True,
        )

    if stu_col is None:
        print(
            f"[p29] 児童生徒数の列が無い。全件を既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人"
            "として扱う（--assume-missing）。規模による重み付けは効かない",
            file=sys.stderr,
        )
        students = pd.Series(SCHOOL_STUDENTS_FALLBACK, index=gdf.index)
    else:
        students = pd.to_numeric(gdf[stu_col], errors="coerce")
        missing = int(students.isna().sum())
        if missing:
            print(
                f"[p29] {stu_col} が空の {missing:,}/{len(gdf):,}件は"
                f"既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人で補う",
                file=sys.stderr,
            )
        students = students.fillna(SCHOOL_STUDENTS_FALLBACK)

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


def normalize_welfare(path: Path, clip: bool = True) -> gpd.GeoDataFrame:
    """国土数値情報 P14 福祉施設から障害福祉サービス事業所を抽出する。

    P14 は座標の網羅性が高い一方、**定員が無く、種別も分離できない**
    （schema.P14_SUBCLASS のコメント参照）。そこで:

      1. 小分類コードで障害福祉関係だけに絞る（高齢者介護・保育を除外）
      2. 施設名称のキーワードでサービス種別を推定する
      3. 種別ごとの仮定員を当てる

    2 と 3 は WAM NET（実際の種別と定員を持つ）が入るまでの暫定措置。
    推定に頼った件数は必ずログへ出し、どれだけ仮定に依存しているかを可視化する。
    """
    gdf = gpd.read_file(path).to_crs(CRS_GEOGRAPHIC)
    total = len(gdf)
    m = COLUMN_MAP["ksj_p14_welfare"]

    sub_col = require_column(
        gdf,
        path,
        "ksj_p14_welfare",
        "subclass",
        tag="p14",
        label="小分類コード",
        why=(
            "このコードで障害福祉関係だけに絞る。特定できないまま進めると"
            "高齢者介護・保育を含む全福祉施設が需要に乗る。"
        ),
        predicate=lambda s: s.astype(str).isin(P14_SUBCLASS),
        min_ratio=0.8,
    )
    # 名称は「表示のため」ではなく **絞り込みと種別推定のため** に要る。
    # 高齢系と混在する小分類コードは名称で振り分けており（name_gated）、
    # 名称が無いとその全件が落ちて需要が大きく過小になる。
    name_col = require_column(
        gdf,
        path,
        "ksj_p14_welfare",
        "name",
        tag="p14",
        label="施設名称",
        why=(
            "高齢者介護と混在する小分類コードは名称で振り分けている。"
            "特定できないまま進めると該当件数が大きく減り、"
            "サービス種別の推定も全件が既定重みになる。"
        ),
        # 「文字列であること」だけを条件にすると都道府県名や分類コードの列を
        # 掴んでしまう（実際に P14_001 を名称と誤認した）。
        # 施設名称は **ほぼ一意** であることを条件に加える。
        predicate=lambda s: s.astype(str).str.len().between(4, 80),
        whole=lambda s: int(s.nunique()) >= max(3, int(len(s) * 0.8)),
        name_hint=r"名称|施設名",
    )

    # --- 1. 障害福祉関係だけに絞る ---
    # P14 は障害福祉と高齢者介護を分類コードで分離できない
    # （schema.P14_SUBCLASS の冒頭コメント参照）。
    # そのため「コードで断定できるもの」＋「名称が障害系のもの」だけを残し、
    # 判断できないものは落とす。取りこぼしは許すが混入は許さない。
    codes = gdf[sub_col].astype(str)
    names_all = gdf[name_col].astype(str)

    unknown = sorted(set(codes) - set(P14_SUBCLASS))
    if unknown:
        print(
            f"[p14] 未知の小分類コード {len(unknown)} 種は安全側に倒して除外: "
            f"{unknown[:8]} — 障害福祉関係なら schema.P14_SUBCLASS へ追加すること",
            file=sys.stderr,
        )

    scope = codes.map(
        lambda c: P14_SUBCLASS.get(c, ("", P14_SUBCLASS_DEFAULT_SCOPE))[1]
    )
    gated_hit = names_all.map(is_disability_name)
    keep = (scope == "include") | ((scope == "name_gated") & gated_hit)

    n_gated_total = int((scope == "name_gated").sum())
    n_gated_kept = int(((scope == "name_gated") & gated_hit).sum())
    gdf = gdf[keep].copy()

    print(
        f"[p14] {total:,}件 → 障害福祉と判定 {len(gdf):,}件\n"
        f"       うちコードで断定 {int((scope == 'include').sum()):,}件 / "
        f"名称で判定 {n_gated_kept:,}件\n"
        f"       混在コードの {n_gated_total - n_gated_kept:,}件は"
        f"高齢者介護の可能性があるため除外（取りこぼし込み）"
    )

    if clip:
        gdf = clip_to_study_area(gdf)
        print(f"[p14] 研究領域内 {len(gdf):,}件")

    if len(gdf) == 0:
        raise ValueError("研究領域内に該当施設が 0 件。STUDY_BBOX を確認すること。")

    # --- 2. 名称からサービス種別を推定 ---
    names = gdf[name_col].astype(str)
    classified = names.map(classify_welfare_by_name)
    gdf["kind"] = [c[0] for c in classified]
    inferred = sum(1 for c in classified if c[1])
    print(
        f"[p14] 種別を名称から推定: {inferred:,}/{len(gdf):,}件 "
        f"({inferred / len(gdf) * 100:.0f}%)。残りは既定重みで扱う"
    )

    # --- 3. 仮定員を当てる ---
    gdf["capacity"] = gdf["kind"].map(assumed_capacity)
    gdf["weight"] = gdf["kind"].map(welfare_weight)

    out = gpd.GeoDataFrame(
        {
            "name": names,
            "kind": gdf["kind"],
            "subclass": gdf[sub_col].astype(str),
            "capacity": gdf["capacity"],
            "capacity_estimated": True,  # WAM NET が入れば False になる
            "weight": gdf["weight"],
            "demand_value": gdf["capacity"] * gdf["weight"],
            "source": SOURCES["ksj_p14_welfare"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y

    top = out["kind"].value_counts().head(6)
    print("[p14] 推定された種別の内訳:")
    for kind, n in top.items():
        print(f"        {kind:<28} {n:4d}件 (重み {welfare_weight(kind):.2f})")

    return out.reset_index(drop=True)


def normalize_hosts_from_p14(path: Path, clip: bool = True) -> gpd.GeoDataFrame:
    """P14 からホスト施設候補（児童館等）を抽出する。

    実データを見て分かった副産物。児童館は東京都に 587 件あり、
    区市町村ごとにバラバラな公共施設一覧を集める前に、
    提言の割当先をひとまず全都分そろえられる。
    """
    gdf = gpd.read_file(path).to_crs(CRS_GEOGRAPHIC)
    m = COLUMN_MAP["ksj_p14_welfare"]
    sub_col = require_column(
        gdf,
        path,
        "ksj_p14_welfare",
        "subclass",
        tag="p14",
        label="小分類コード",
        why="このコードで児童館等のホスト施設候補に絞る。",
        predicate=lambda s: s.astype(str).isin(P14_SUBCLASS),
        min_ratio=0.8,
    )
    name_col = pick_column(gdf, m["name"])
    city_col = pick_column(gdf, m["city"])

    total = len(gdf)
    gdf = gdf[gdf[sub_col].astype(str).isin(P14_HOST_SUBCLASS)].copy()
    if clip:
        gdf = clip_to_study_area(gdf)
    require_nonempty(
        len(gdf),
        tag="p14",
        what=f"ホスト施設候補（{sub_col} が {sorted(P14_HOST_SUBCLASS)} のもの）",
        why=(
            f"{total:,}件を読んだが該当が無い。都全体のファイルか、"
            f"{sub_col} が小分類コードの列かを確認すること。"
        ),
    )

    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col].astype(str) if name_col else "",
            "host_kind": gdf[sub_col].astype(str).map(P14_HOST_SUBCLASS),
            "ward": gdf[city_col].astype(str) if city_col else "",
            "source": SOURCES["ksj_p14_welfare"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry,
        crs=CRS_GEOGRAPHIC,
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y
    print(f"[p14] ホスト施設候補 {len(out):,}件")
    return out.reset_index(drop=True)


def normalize_wamnet(
    path: Path, geocode: bool = False, assume_missing: bool = False
) -> gpd.GeoDataFrame:
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

    name_col = optional_column(
        df,
        "wamnet_jigyosho",
        "name",
        tag="wamnet",
        label="事業所名称",
        fallback="根拠カードに事業所名が出ない",
    )
    # WAM NET を使う理由そのものが「サービス種別と定員を持つ」ことなので、
    # この 2 列は取り違えたら止める。既定値で埋めると P14 と同じ精度に戻り、
    # しかも「実データを入れた」と表示される。
    kind_col = require_column(
        df,
        path,
        "wamnet_jigyosho",
        "kind",
        tag="wamnet",
        label="サービス種別",
        why=(
            "需要重み（就労移行・生活介護・放課後等デイ等）は種別ごとに"
            "大きく違う。特定できないまま進めると全件が既定重みになり、"
            "P14 より精度が上がらないまま実データとして表示される。"
        ),
        predicate=lambda s: s.astype(str).map(normalize_welfare_type).astype(bool),
        min_ratio=0.5,
        name_hint=r"サービス|種別|種類",
    )
    cap_col = pick_column(df, m["capacity"])
    if cap_col is None and not assume_missing:
        cap_col = require_column(
            df,
            path,
            "wamnet_jigyosho",
            "capacity",
            tag="wamnet",
            label="定員",
            why=(
                "P14 に定員が無いため WAM NET を必須ソースに格上げした。"
                "その定員を特定できないまま進めると全件が既定値 "
                f"{WAMNET_CAPACITY_FALLBACK:.0f}人になり、"
                "P14 の仮定員と同じ状態に戻る。\n"
                "定員が本当に収録されていない年度なら --assume-missing を付ける。"
            ),
            # 定員も「ただの正の整数」。列名の手掛かりが無ければ推定しない。
            predicate=lambda s: pd.to_numeric(s, errors="coerce").between(1, 2000),
            min_ratio=0.5,
            whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 3,
            name_hint=r"定員|利用者数",
            require_hint=True,
        )
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
    require_nonempty(
        len(gdf),
        tag="wamnet",
        what="研究領域内の事業所",
        why=f"{len(df):,}件を読んだが対象 2 区に入るものが無い。",
    )

    kinds = gdf[kind_col].astype(str).map(normalize_welfare_type)
    unmapped = int((kinds == "").sum())
    if unmapped:
        samples = list(gdf.loc[kinds == "", kind_col].unique()[:5])
        print(
            f"[wamnet] {kind_col} を種別に対応付けられない {unmapped:,}/{len(gdf):,}件は"
            f"既定重みで扱う: {samples} — schema の対応表へ追加すること",
            file=sys.stderr,
        )

    if cap_col is None:
        print(
            f"[wamnet] 定員の列が無い。全件を既定値 "
            f"{WAMNET_CAPACITY_FALLBACK:.0f}人として扱う（--assume-missing）。"
            "WAM NET を使う利点の半分（実定員）は得られていない",
            file=sys.stderr,
        )
        capacity = pd.Series(WAMNET_CAPACITY_FALLBACK, index=gdf.index)
    else:
        capacity = pd.to_numeric(gdf[cap_col], errors="coerce")
        blank = int(capacity.isna().sum())
        if blank:
            print(
                f"[wamnet] {cap_col} が空の {blank:,}/{len(gdf):,}件は"
                f"既定値 {WAMNET_CAPACITY_FALLBACK:.0f}人で補う（訪問系は定員なし）",
                file=sys.stderr,
            )
        capacity = capacity.fillna(WAMNET_CAPACITY_FALLBACK)
        print(
            f"[wamnet] 研究領域内 {len(gdf):,}件 / "
            f"定員 {capacity.min():.0f}〜{capacity.max():.0f}人（{cap_col}）"
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
    """国土数値情報 P04 から精神科・心療内科を抽出する。

    診療科目の列を取り違えると、**全医療機関が精神科・心療内科として**
    需要に乗る。渋谷区・世田谷区の医療機関は千件規模あり、
    絞り込みが外れたことは件数を見ても気付きにくい。
    """
    gdf = clip_to_study_area(gpd.read_file(path).to_crs(CRS_GEOGRAPHIC))
    total = len(gdf)
    require_nonempty(
        total,
        tag="p04",
        what="研究領域内の医療機関",
        why="対象範囲か入力を確認すること。",
    )

    pattern = "|".join(PSYCH_CLINIC_KEYWORDS)
    # 診療科目欄は「内科・外科・整形外科」のように連結されている。
    # 中身から探すときは、よくある診療科名を含む列を目印にする。
    dep_hint = "|".join(
        ("内科", "外科", "小児科", "皮膚科", "眼科", "耳鼻", "産婦人科", "歯科")
        + PSYCH_CLINIC_KEYWORDS
    )
    dep_col = require_column(
        gdf,
        path,
        "ksj_p04_medical",
        "departments",
        tag="p04",
        label="診療科目",
        why=(
            "この列で精神科・心療内科に絞る。特定できないまま進めると"
            f"研究領域内の全 {total:,} 件が精神科として需要に乗る。"
        ),
        predicate=lambda s: s.astype(str).str.contains(dep_hint, na=False),
        min_ratio=0.5,
        name_hint=r"科|診療",
    )
    name_col = optional_column(
        gdf,
        "ksj_p04_medical",
        "name",
        tag="p04",
        label="医療機関名",
        fallback="根拠カードに施設名が出ない",
    )

    gdf = gdf[gdf[dep_col].astype(str).str.contains(pattern, na=False)]
    require_nonempty(
        len(gdf),
        tag="p04",
        what=f"精神科・心療内科（{dep_col} に {'/'.join(PSYCH_CLINIC_KEYWORDS)} を含む行）",
        why=(
            f"研究領域内 {total:,}件のいずれも該当しない。"
            f"{dep_col} が診療科目の列か確認すること: "
            f"python -m etl.fetch --inspect {path}"
        ),
    )
    print(f"[p04] {total:,}件 → 精神科・心療内科 {len(gdf):,}件（{dep_col} で判定）")

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


def _extract_zoning_codes(gdf: gpd.GeoDataFrame, path: Path) -> pd.Series:
    """A29 から用途地域コードを取り出す。

    列名も格納形式も年度で変わる。仕様書からの推測で 1 列だけ決め打ちすると、
    外れたときに **全件 NaN のまま静かに通ってしまう**（既定値 0.30 が
    全メッシュに乗り、負荷スコアの主軸が消える）。
    実際に A29-19_13 でこれが起きた。

    そこで全列を走査し、
      1. 用途地域コード（1〜13）として解釈できる列
      2. 用途地域名（「商業地域」等）として解釈できる列
    のどちらかが見つかるまで探す。どちらも無ければ、
    各列の実際の値を添えて失敗する。黙って 0.30 を返すよりよい。
    """
    name_to_code = {name: code for code, name in ZONING_NAME.items()}
    valid = set(ZONING_LOAD)

    # --- 1. コードとして解釈できる列を探す ---
    best: tuple[str, pd.Series, float] | None = None
    for col in gdf.columns:
        if col == "geometry":
            continue
        nums = pd.to_numeric(gdf[col], errors="coerce")
        hit = nums.isin(valid)
        ratio = float(hit.mean())
        # 用途地域コードは 1〜13 に収まり、ほぼ全件が有効値になるはず。
        # 行政区域コード(13113)や建蔽率(60)・容積率(400)は valid に入らず弾かれる。
        #
        # ただし都道府県コードは東京都なら全件 13 で、これは有効値の範囲に
        # 入ってしまう（13 = 田園住居地域）。用途地域が 1 種類しかない
        # 都市計画区域は現実には無いので、値の種類数で除外する。
        if ratio > 0.9 and int(nums[hit].nunique()) >= 3:
            if best is None or ratio > best[2]:
                best = (col, nums, ratio)

    if best is not None:
        col, nums, ratio = best
        print(f"[a29] 用途地域コード列 = {col}（有効値 {ratio * 100:.0f}%）")
        return nums

    # --- 2. 名称として解釈できる列を探す ---
    for col in gdf.columns:
        if col == "geometry":
            continue
        mapped = gdf[col].astype(str).str.strip().map(name_to_code)
        ratio = float(mapped.notna().mean())
        if ratio > 0.9:
            print(f"[a29] 用途地域名の列 = {col}（有効値 {ratio * 100:.0f}%）")
            return mapped.astype("Float64").astype(float)

    # --- どちらも見つからない ---
    lines = [
        "用途地域コードを特定できない。",
        "コード(1〜13)としても名称としても解釈できる列が無かった。",
        "",
        "各列の実際の値:",
    ]
    for col in gdf.columns:
        if col == "geometry":
            continue
        vals = gdf[col].dropna().unique()[:8]
        lines.append(f"    {col}: {list(vals)}")
    lines += [
        "",
        "用途地域を表す列を見つけて、",
        "etl/fetch.py の COLUMN_MAP['ksj_a29_youto']['zoning_code'] に追加すること。",
        f"詳細: python -m etl.fetch --inspect {path}",
    ]
    raise ValueError("\n".join(lines))


def normalize_zoning(path: Path, clip: bool = True) -> gpd.GeoDataFrame:
    """国土数値情報 A29 用途地域を負荷スコアつきポリゴンにする。

    A29 は都市計画区域ごとに分割配布されることがあるため、
    ファイル 1 本でもフォルダでも受け取れる（read_vector が結合する）。
    """
    gdf = read_vector(path).to_crs(CRS_GEOGRAPHIC)
    total = len(gdf)
    if clip:
        gdf = clip_to_study_area(gdf)
    print(f"[a29] {total:,}件 → 研究領域内 {len(gdf):,}件")
    if len(gdf) == 0:
        raise ValueError("研究領域内に用途地域が 0 件。対象範囲か入力を確認すること。")

    codes = _extract_zoning_codes(gdf, path)

    unknown = sorted(set(codes.dropna().astype(int)) - set(ZONING_LOAD))
    if unknown:
        print(
            f"[a29] 未知の用途地域コード {unknown} は既定値 {ZONING_LOAD_DEFAULT} で扱う",
            file=sys.stderr,
        )

    out = gpd.GeoDataFrame(
        {
            "zoning_code": codes,
            "zoning_load": codes.map(ZONING_LOAD).fillna(ZONING_LOAD_DEFAULT),
            "source": SOURCES["ksj_a29_youto"].label,
            "synthetic": False,
        },
        geometry=gdf.geometry,
        crs=CRS_GEOGRAPHIC,
    ).reset_index(drop=True)

    print("[a29] 用途地域の内訳:")
    for code, n in codes.value_counts().head(13).items():
        name = ZONING_NAME.get(int(code), "不明")
        load = ZONING_LOAD.get(int(code), ZONING_LOAD_DEFAULT)
        print(f"        {name:<22} {n:5d}件 (負荷 {load:.2f})")
    return out


def normalize_noise(path: Path) -> gpd.GeoDataFrame:
    """東京都環境局の騒音測定結果を点データにする。

    騒音レベルの列を取り違えると全件 NaN になり、
    測定点が存在するのに騒音レイヤーの値が消える
    （「点を面に変換する」という本作の主張ごと消える）。
    """
    df = read_csv_japanese(path)
    lon_col = require_column(
        df,
        path,
        "tokyo_road_noise",
        "lon",
        tag="noise",
        label="経度",
        why="測定点を地図に載せるために必須。",
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(*TOKYO_LON_RANGE),
        name_hint=r"経度|lon",
    )
    lat_col = require_column(
        df,
        path,
        "tokyo_road_noise",
        "lat",
        tag="noise",
        label="緯度",
        why="測定点を地図に載せるために必須。",
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(*TOKYO_LAT_RANGE),
        name_hint=r"緯度|lat",
    )
    laeq_col = require_column(
        df,
        path,
        "tokyo_road_noise",
        "laeq_db",
        tag="noise",
        label="等価騒音レベル(昼間 LAeq)",
        why=(
            "この値を距離重み付き内挿して面にする。特定できないまま進めると"
            "測定点だけがあって騒音の値が全件空になる。"
        ),
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(*NOISE_DB_RANGE),
        min_ratio=0.8,
        whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 5,
        # 昼間と夜間はどちらも同じ値域に入る。昼間を優先する
        # （要請限度の評価も日常の滞在も昼間が主）。
        name_hint=r"昼",
    )
    name_col = optional_column(
        df,
        "tokyo_road_noise",
        "name",
        tag="noise",
        label="測定地点名",
        fallback="根拠カードに地点名が出ない",
    )

    gdf = clip_to_study_area(_to_points(df, lon_col, lat_col))
    require_nonempty(
        len(gdf),
        tag="noise",
        what="研究領域内の測定点",
        why=(
            f"{len(df):,}件を読んだが対象 2 区に入るものが無い。"
            f"{lat_col}/{lon_col} が緯度経度か、"
            "都全体のファイルか（区が違う）を確認すること。"
        ),
    )
    gdf["laeq_db"] = pd.to_numeric(gdf[laeq_col], errors="coerce")
    blank = int(gdf["laeq_db"].isna().sum())
    if blank:
        print(
            f"[noise] {laeq_col} が空の測定点 {blank:,}/{len(gdf):,}件は内挿に使えない",
            file=sys.stderr,
        )
    require_nonempty(
        len(gdf) - blank,
        tag="noise",
        what=f"騒音レベルを読めた測定点（{laeq_col}）",
        why=f"数値として解釈できる行が無い。実際の値: {list(gdf[laeq_col].head(5))}",
    )
    print(
        f"[noise] 研究領域内 {len(gdf):,}件 / "
        f"{laeq_col} = {gdf['laeq_db'].min():.0f}〜{gdf['laeq_db'].max():.0f} dB"
    )
    gdf["name"] = gdf[name_col] if name_col else ""
    gdf["source"] = SOURCES["tokyo_road_noise"].label
    gdf["synthetic"] = False
    return gdf[
        ["name", "laeq_db", "lon", "lat", "source", "synthetic", "geometry"]
    ].reset_index(drop=True)


def normalize_hosts(path: Path) -> gpd.GeoDataFrame:
    """公共施設一覧をホスト施設候補にする。

    ホスト種別は施設名から判定する（schema.HOST_TYPE_PATTERNS）。
    つまり **名称の列を取り違えると候補が 0 件になり、
    「既存施設では到達不可」の件数が実態より多く出る** ——
    しかもレイヤーは実データとして数えられるので警告も出ない。
    区ごとに様式が違うデータなので、列名の決め打ちが最も外れやすい。
    """
    df = read_csv_japanese(path)
    lon_col = require_column(
        df,
        path,
        "tokyo_public_facility",
        "lon",
        tag="facilities",
        label="経度",
        why="提言の宛先として地図に載せるために必須。",
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(*TOKYO_LON_RANGE),
        name_hint=r"経度|lon",
    )
    lat_col = require_column(
        df,
        path,
        "tokyo_public_facility",
        "lat",
        tag="facilities",
        label="緯度",
        why="提言の宛先として地図に載せるために必須。",
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(*TOKYO_LAT_RANGE),
        name_hint=r"緯度|lat",
    )
    name_col = require_column(
        df,
        path,
        "tokyo_public_facility",
        "name",
        tag="facilities",
        label="施設名",
        why=(
            "施設名からホスト種別（図書館・区民センター等）を判定する。"
            "特定できないまま進めると候補が 0 件になり、"
            "「既存施設では到達不可」の件数が実態より多く出る。"
        ),
        # 中身から探す場合は「ホスト種別として判定できる名前が並んでいる列」を
        # 目印にする。一覧には対象外の施設（学校・保育園）も含まれるため、
        # 全件一致は期待できない。
        predicate=lambda s: s.astype(str).map(host_type).notna(),
        min_ratio=0.15,
        name_hint=r"施設|名称|名$",
    )
    ward_col = optional_column(
        df,
        "tokyo_public_facility",
        "ward",
        tag="facilities",
        label="区市町村",
        fallback="提言の宛先に区名が出ない",
    )

    gdf = clip_to_study_area(_to_points(df, lon_col, lat_col))
    require_nonempty(
        len(gdf),
        tag="facilities",
        what="研究領域内の公共施設",
        why=(
            f"{len(df):,}件を読んだが対象 2 区に入るものが無い。"
            f"{lat_col}/{lon_col} が緯度経度か確認すること。"
        ),
    )
    gdf["name"] = gdf[name_col]
    gdf["host_kind"] = gdf["name"].map(host_type)
    gdf["ward"] = gdf[ward_col] if ward_col else ""
    gdf["source"] = SOURCES["tokyo_public_facility"].label
    gdf["synthetic"] = False
    # ホスト種別を判定できない施設（学校・保育園等）は候補から外す。
    kept = gdf[gdf["host_kind"].notna()]
    require_nonempty(
        len(kept),
        tag="facilities",
        what=f"ホスト種別を判定できた施設（{name_col} から判定）",
        why=(
            f"研究領域内 {len(gdf):,}件のどれも種別に一致しない。"
            f"{name_col} が施設名の列か、"
            "schema.HOST_TYPE_PATTERNS に無い名称ばかりでないか確認すること。\n"
            f"    {name_col} の実際の値: {list(gdf['name'].head(5))}"
        ),
    )
    gdf = kept
    print(f"[facilities] 研究領域内 {len(gdf):,}件（{name_col} で種別を判定）")
    for kind, n in gdf["host_kind"].value_counts().items():
        print(f"        {kind:<16} {n:4d}件")
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
    """data/processed にある実データレイヤーだけを読む。

    全部そろっている必要はない。オープンデータは 1 本ずつしか片付かないので、
    「落とせた分だけ実データ、残りは模擬データ」で地図を更新できる方が
    作業が進む。どのレイヤーが実データかは build.py が集計して表示する。
    """
    layers: dict = {}
    for key, filename in PROCESSED_FILES.items():
        path = DATA_PROCESSED / filename
        if path.exists():
            layers[key] = gpd.read_file(path)

    pop_path = DATA_PROCESSED / "population.csv"
    if pop_path.exists():
        layers["population"] = read_csv_japanese(pop_path)
    return layers


# 落としたファイルを正規化して data/processed へ置くための対応表。
#   python -m etl.fetch --normalize p14 data/raw/P14-21_13.geojson
NORMALIZERS: dict[str, tuple[str, str]] = {
    "n03": ("area", "normalize_area"),
    "p14": ("welfare", "normalize_welfare"),
    "p14-hosts": ("hosts", "normalize_hosts_from_p14"),
    "wamnet": ("welfare", "normalize_wamnet"),
    "p29": ("schools", "normalize_schools"),
    "p04": ("clinics", "normalize_clinics"),
    "a29": ("zoning", "normalize_zoning"),
    "noise": ("noise", "normalize_noise"),
    "facilities": ("hosts", "normalize_hosts"),
}


def run_normalizer(kind: str, path: Path, assume_missing: bool = False) -> Path:
    """指定した正規化を実行し、data/processed へ書き出す。

    assume_missing は「その列が本当に収録されていない年度」のための逃げ道。
    受け付ける正規化（生徒数・定員）にだけ渡す。既定では渡さない ——
    列を取り違えたときに既定値で埋めて通してしまうのを防ぐのが目的なので、
    逃げ道は明示的に指定したときだけ開く。
    """
    if kind not in NORMALIZERS:
        raise SystemExit(
            f"未知の種別 {kind!r}。使えるのは: {', '.join(sorted(NORMALIZERS))}"
        )
    layer_key, func_name = NORMALIZERS[kind]
    func = globals()[func_name]
    kwargs = {}
    if assume_missing:
        if "assume_missing" not in inspect.signature(func).parameters:
            raise SystemExit(
                f"--assume-missing は種別 {kind!r} には無い。"
                "既定値で代用できる列を持つのは p29（生徒数）と wamnet（定員）だけ。"
            )
        kwargs["assume_missing"] = True
    gdf = func(path, **kwargs)
    out = DATA_PROCESSED / PROCESSED_FILES[layer_key]
    gdf.to_file(out, driver="GeoJSON")
    print(f"\n[normalize] {out.relative_to(out.parents[2])} に {len(gdf):,}件を書き出した")
    print("次: python -m etl.build --live")
    return out


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="オープンデータの取得と正規化")
    ap.add_argument("--check", action="store_true", help="到達性の確認のみ")
    ap.add_argument("--force", action="store_true", help="キャッシュを無視して再取得")
    ap.add_argument("--inspect", type=Path, help="落としたファイルの列名を表示")
    ap.add_argument(
        "--assume-missing",
        action="store_true",
        help="列が本当に収録されていない年度のときだけ既定値で代用する"
        "（p29 の生徒数 / wamnet の定員）",
    )
    ap.add_argument(
        "--normalize",
        nargs=2,
        metavar=("種別", "ファイル"),
        help=f"正規化して data/processed へ書き出す。種別: {', '.join(sorted(NORMALIZERS))}",
    )
    args = ap.parse_args(argv)

    if args.inspect:
        inspect_columns(args.inspect)
        return 0

    if args.normalize:
        kind, path = args.normalize
        run_normalizer(kind, Path(path), assume_missing=args.assume_missing)
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
