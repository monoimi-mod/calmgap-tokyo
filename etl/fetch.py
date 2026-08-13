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
import math
import os
import re
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from . import geocode
from .config import (
    CLIP_BUFFER_M,
    CRS_GEOGRAPHIC,
    CRS_PROJECTED,
    current_source_label,
    DATA_PROCESSED,
    DATA_RAW,
    NOISE_GIS_YEARS,
    SOURCES,
    SOURCE_JOIN,
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
    PSYCH_CLINIC_ABBREV,
    PSYCH_CLINIC_KEYWORDS,
    SCHOOL_CLASS_SPECIAL_NEEDS,
    SCHOOL_FOUNDER_NAME_PATTERNS,
    SCHOOL_NAME_PATTERN,
    SCHOOL_STUDENTS_SELF_REPORTED,
    SCHOOL_STUDENTS_UNKNOWN_LABEL,
    SPECIAL_NEEDS_NAME_PATTERN,
    normalize_school_name,
    ZONING_LOAD,
    ZONING_LOAD_DEFAULT,
    host_type,
    is_psych_clinic,
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
    # 実データ P29-23_13（東京都・2023年版＝製品仕様書第2.0版）で確認済み。
    #   P29_001 行政区域コード / P29_002 学校コード   / P29_003 学校分類コード
    #   P29_004 名称           / P29_005 所在地       / P29_006 管理者コード
    #   P29_009 キャンパス名
    # 第1.1版（P29-13）は列の意味がずれており、**同じ列名が別のものを指す**:
    #   P29_003 施設種別詳細（盲16008/聾16009/養護16010）/ P29_004 学校分類コード
    #   P29_005 名称
    # そのため列名だけで選ぶと、第1.1版で P29_003 を掴んで特別支援学校 67 件が
    # 9 件になる（取りこぼす方向なので出力を見ても気付けない）。
    # normalize_schools は名称の列で裏を取ってから分類コード列を決める。
    "ksj_p29_school": {
        "class_code": ("P29_003", "P29_004"),
        "name": ("P29_004", "P29_005"),
        # 児童生徒数は第1.1版・第2.0版のどちらにも無い。仕様書から推測した
        # P29_009 は第2.0版ではキャンパス名で、数値化すると全件 NaN になる。
        # 将来の版が持つなら列名をここへ足す。無い年度は --assume-missing。
        "students": ("児童生徒数", "生徒数"),
        # 設置者コード。国立・私立は都教委の在籍者数調査の対象外なので、
        # 「一致しなかった」のか「元々載らない」のかをこれで切り分ける。
        "founder": ("P29_006",),
    },
    # 東京都教育委員会 公立学校統計調査報告書【東京都公立学校一覧】の
    # 「特別支援学校（学校別在籍者数）」CSV。列名は 1 行目に平坦に並ぶ
    # （Excel 版は 6 段のセル結合ヘッダなので CSV 版を使う）。
    #   学校番号 / 設置者 / 障害種別 / 併置校 / 学校名 / 在籍者数/総数 / …
    "tokyo_sped_enrollment": {
        "school_id": ("学校番号",),
        "name": ("学校名",),
        "students": ("在籍者数/総数", "在籍者数／総数", "在籍者数"),
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
    # 実データ P04-20_13（東京都・2020年版）で確認済み。
    #   P04_001 医療機関分類（1 病院 / 2 診療所 / 3 歯科診療所）
    #   P04_002 名称 / P04_003 所在地 / P04_004 診療科目 / P04_005 その他の診療科目
    # 推測していた P04_003 は所在地だった（診療科目ではない）。
    "ksj_p04_medical": {
        "name": ("P04_002",),
        "departments": ("P04_004", "P04_003"),
    },
    # 実データ A29-19_13（東京都・2019年版）で確認済み。
    # 用途地域コードは **A29_004**。当初 A29_005 と推測していたが外れており、
    # 全件 NaN のまま既定値 0.30 が乗って静かに壊れていた。
    # 現在は _extract_zoning_codes が全列を走査して自動判定するので、
    # ここは「確認済みの列を先頭に置く」記録としての意味を持つ。
    "ksj_a29_youto": {
        "zoning_code": ("A29_004", "A29_005"),
    },
    # 実列名は WAM NET オープンデータ（2026年3月末・サービス種別ごとに 29 分割）で確認した。
    "wamnet_jigyosho": {
        "name": ("事業所名称", "事業所名", "事業所の名称"),
        "kind": ("サービス種別", "サービスの種類"),
        "capacity": ("定員", "利用定員"),
        "address": ("事業所住所", "所在地", "事業所所在地", "事業所住所（市区町村）"),
        "lat": ("緯度", "事業所緯度"),
        "lon": ("経度", "事業所経度"),
    },
    # 実データ 平成25年度 自動車交通騒音調査結果（東京都環境局・673 地点）で確認済み。
    # 昼夜の等価騒音レベルが並んで入る（列名の末尾に単位が付く）。
    # 地点名の列は無く、住所で代用する。
    # 実データ S12-25（全国・2025年版）で確認済み。1 行が「駅×事業者×路線」。
    #   S12_001 駅名 / S12_001c 駅コード / S12_001g **グループコード**
    #   S12_002 運営会社 / S12_003 路線名
    #   以降 2011〜2024 年の 4 列組（重複コード・データ有無・備考・**乗降客数**）
    #   最新年 2024 の乗降客数は S12_061。
    "ksj_s12_station": {
        "name": ("S12_001", "駅名"),
        "passengers": ("S12_061", "乗降客数2024"),
        "group": ("S12_001g", "グループコード"),
    },
    # 実データ P13-11_13（東京都・2011年版）で確認済み。
    #   P13_003 名称 / P13_004 公園種別 / P13_006 所在地 / P13_007 供用開始年
    #   P13_008 **面積(m²)** / 形状は入っておらず点で表現される
    "ksj_p13_park": {
        "area": ("P13_008", "面積"),
        "name": ("P13_003", "名称", "公園名"),
    },
    "tokyo_road_noise": {
        # 「等価騒音レベル(dB)昼間」は要請限度の表（見出しが 2 段で、
        # 平坦化すると単位が先に来る）。常時監視は「昼間等価騒音レベル(dB)」。
        "laeq_db": (
            "昼間等価騒音レベル(dB)",
            "昼間等価騒音レベル",
            "等価騒音レベル(dB)昼間",
            # 環境GIS＋（全国の常時監視結果）の列名。
            "騒音_昼間(dB)",
            "LAeq昼間",
        ),
        # 台東区は「X座標」「Y座標」。**どちらが緯度かは出典で変わる**——
        # 平面直角座標系なら X が北（緯度相当）だが、台東区は X が経度である。
        # そのため対応づけは推測で置き、`resolve_column` の値域検査に判定を委ねる
        # （範囲外なら採らず、中身から探し直す）。候補に入れておく理由は、
        # 無いと `_fill_coords_from_address` が先に走り、区名の無い住所
        # （「三ノ輪1丁目27番11号」）で 0〜42% しか当たらないジオコーディングを
        # 無駄に回した上、ログが「住所から座標化」と誤解を招く形で出るため。
        "lat": ("緯度", "Y座標"),
        "lon": ("経度", "X座標"),
        "name": ("測定地点の住所", "測定地点住所", "測定地点", "地点名"),
        # 常時監視の測定地点は**緯度経度を持たない**。住所しか無いので
        # `etl/geocode.py`（位置参照情報）で座標化する。名称の列と同じものだが、
        # 役割が違うので別の欄にしてある——`name` を取り違えても表示が
        # 崩れるだけだが、`address` を取り違えると座標が全件外れる。
        "address": ("測定地点の住所", "測定地点住所", "測定地点"),
        # 環境基準の地域類型（A/AA/B/C）。**この列は表示に使わない**——
        # 「読ませようとしている表が常時監視か」を中身で確かめるためだけに引く
        # （`_pick_monitoring_table`）。
        "area_type": ("環境基準類型", "区域の区分"),
        # 測定年度を中身から取るための列。**ファイル名から年度を当てない**
        # （手元のファイル名は `cyousakekka$300500a...files$2019monitoring.csv`
        #  のようにブラウザが化けさせた形で届く）。
        "surveyed": (
            "測定開始年月日",
            "測定年月日開始",
            "測定期間開始",
            # 環境GIS＋の列名。
            "測定開始日",
        ),
    },
    "tokyo_public_facility": {
        # 「事業所名」は渋谷区（SHIBUYA OPEN DATA の施設・事業所一覧）、
        # 「名称」は自治体標準オープンデータセット（世田谷区・新宿区）の列名。
        # 「ページタイトル」は港区。施設ページの見出しがそのまま施設名になっている。
        # 「列1」は江東区。**自治体標準オープンデータセットの様式のまま、
        # 名称の列だけ見出しが表計算の既定値に化けている**（617 行）。
        # 位置（4 列目）で当てるのは P29 の轍なので、中身で裏を取ってから足した:
        #   - 一意率 1.00。「小区分」0.06・「担当課」0.04 とは桁が違う（whole= で弾く）
        #   - `host_type` の一致率 9.2% で全 59 列中の最大
        #   - **文字数が「名称_カナ」の文字数と r=+0.882 で相関する。**
        #     同じ様式の「名称_カナ」「名称_英字」「名称_通称」は正しい見出しで
        #     残っており、カナ読み（コウトウクヤクショ）が名称の裏付けになる。
        #     他の高一意率列（ID・所在地）は相関しない
        # 候補の最後に置く。「名称」がある出典ではそちらが先に当たる。
        "name": ("施設名", "名称", "施設名称", "事業所名", "ページタイトル", "列1"),
        # 台東区は「X座標」「Y座標」。**どちらが緯度かは出典で変わる**——
        # 平面直角座標系なら X が北（緯度相当）だが、台東区は X が経度である。
        # そのため対応づけは推測で置き、`resolve_column` の値域検査に判定を委ねる
        # （範囲外なら採らず、中身から探し直す）。候補に入れておく理由は、
        # 無いと `_fill_coords_from_address` が先に走り、区名の無い住所
        # （「三ノ輪1丁目27番11号」）で 0〜42% しか当たらないジオコーディングを
        # 無駄に回した上、ログが「住所から座標化」と誤解を招く形で出るため。
        "lat": ("緯度", "Y座標"),
        "lon": ("経度", "X座標"),
        # 「住所」は最後。区名そのものの列がある出典ではそちらを使う。
        # 文京区は区名の列を持たず住所しか無いので、normalize_ward が
        # 23 区名との前方一致で区を取り出す。
        "ward": (
            "区市町村",
            "自治体名",
            "所在地_市区町村",
            "地方公共団体名",
            "所在地_連結表記",
            "住所",
        ),
        # 施設種別の列。**名称推定より確かなので、あればこちらを優先する。**
        # 文京区の集会施設一覧は「アカデミー文京」「本郷会館」のように
        # 名称からは種別を当てられないが、カテゴリ列に「生涯学習施設」
        # 「区民会館」と書いてある。名称だけで判定すると 13 件を取りこぼす。
        # **「第2分類」を「分類」より先に置く。** 港区は両方を持つが、
        # 「分類」は 009003001000 のような数値コードで種別名ではない。
        # 「第1分類」は使わない——「図書館・文化・スポーツ施設」のように
        # 複数種別をまとめた見出しで、屋内プールまで図書館に一致してしまう。
        "category": (
            "カテゴリ",
            "第2分類",
            "種別",
            "施設種別",
            "施設分類",
            "分類",
        ),
        # 緯度経度が無い／空の区のための住所列（etl/geocode.py で座標化する）。
        "address": ("所在地_連結表記", "住所", "所在地", "所在地_住所"),
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


def column_matches(
    s: pd.Series, predicate, *, min_ratio: float, whole: object = None
) -> bool:
    """その列の中身が求めているものかを検査する。detect_column と同じ条件。"""
    try:
        if float(predicate(s).mean()) < min_ratio:
            return False
    except Exception:  # noqa: BLE001 — 判定できない列は不一致とみなす
        return False
    return whole is None or bool(whole(s))


def resolve_column(
    df: pd.DataFrame,
    kind: str,
    field: str,
    *,
    tag: str,
    label: str,
    predicate=None,
    min_ratio: float = 0.9,
    whole: object = None,
    name_hint: str | None = None,
    require_hint: bool = False,
) -> str | None:
    """列名 → 中身の順で列を探す。見つからなければ None。

    **列名が一致しても中身は確かめる。** 国土数値情報 P29 学校は
    製品仕様書 第1.1版と第2.0版で列の意味が入れ替わっており、
    どちらにも `P29_004` `P29_009` が存在する:

        第1.1版  P29_004 = 学校分類コード / P29_005 = 名称
        第2.0版  P29_003 = 学校分類コード / P29_004 = 名称 / P29_009 = キャンパス名

    「仕様書で P29_009 が児童生徒数」という前提のまま第2.0版を読むと、
    列名は確かに実在するのでそのまま通り、キャンパス名を数値に変換して
    **全件 NaN → 既定値 150 人**になる。A29 で一度やった壊れ方と同じで、
    列名の一致だけでは防げない。中身が合わなければ推定へ回す。
    """
    col = pick_column(df, COLUMN_MAP[kind][field])
    if col is not None:
        if predicate is None or column_matches(
            df[col], predicate, min_ratio=min_ratio, whole=whole
        ):
            return col
        print(
            f"[{tag}] 列 {col} は在るが中身が{label}と合わない"
            f"（例: {list(df[col].dropna().unique()[:4])}）。中身から探し直す",
            file=sys.stderr,
        )
    if predicate is None:
        return None
    return detect_column(
        df,
        predicate,
        tag=tag,
        label=label,
        min_ratio=min_ratio,
        whole=whole,
        name_hint=name_hint,
        require_hint=require_hint,
    )


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
    col = resolve_column(
        df,
        kind,
        field,
        tag=tag,
        label=label,
        predicate=predicate,
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
                f"探した列名: {', '.join(candidates) or '（候補なし）'}",
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
# 同一施設とみなす距離。出典が違えば建物重心・正面入口・住所ジオコーディングで
# 数十 m ずれる。100m は「同名の別施設」を潰さずにゆれを吸収できる幅。
DEDUPE_DISTANCE_M = 100.0
# 列が特定できないときの既定値（--assume-missing でのみ使う）。
SCHOOL_STUDENTS_FALLBACK = 150.0
WAMNET_CAPACITY_FALLBACK = 20.0


# 複数ファイルを並べて正規化するときだけ run_normalizer が立てる。
# 分割された 1 ファイルが 0 件なのは、取り違えではなく「その種別の事業所が
# この 2 区に無い」だけのことがある（WAM NET の療養介護は都内 13 件で、
# 渋谷区・世田谷区には 1 件も無い）。総数が 0 なら run_normalizer が止める。
ALLOW_EMPTY_PER_FILE = False


class EmptyInThisFile(Exception):
    """このファイルには対象範囲の行が無い（複数ファイル指定時のみ使う）。"""


def require_nonempty(
    n: int, *, tag: str, what: str, why: str, per_file: bool = False
) -> None:
    """絞り込みの結果が 0 件なら止める。

    列を取り違えると「条件に合う行が 1 件も無い」形で現れることがある。
    0 件のレイヤーは、実データとして数えられたまま構成要素を消してしまう。

    per_file=True は「対象範囲に入る件数」の検査に付ける。複数ファイルを
    並べたときに限り警告へ落とす（上の ALLOW_EMPTY_PER_FILE）。
    列の取り違えを検査するものには付けない。
    """
    if n:
        return
    if per_file and ALLOW_EMPTY_PER_FILE:
        print(f"[{tag}] {what}が 0 件。このファイルは飛ばす", file=sys.stderr)
        raise EmptyInThisFile(f"[{tag}] {what}が 0 件")
    raise ValueError(f"[{tag}] {what}が 0 件。{why}")


# 東京 23 区。住所から区を取り出すときの照合表。
# 部分一致ではなく前方一致で使う（normalize_ward 参照）。
TOKYO_23_WARDS: tuple[str, ...] = (
    "千代田区", "中央区", "港区", "新宿区", "文京区", "台東区", "墨田区",
    "江東区", "品川区", "目黒区", "大田区", "世田谷区", "渋谷区", "中野区",
    "杉並区", "豊島区", "北区", "荒川区", "板橋区", "練馬区", "足立区",
    "葛飾区", "江戸川区",
)


def normalize_ward(value: object) -> str:
    """区市町村名を「渋谷区」の形へ揃える。

    出典によって「渋谷区」「東京都渋谷区」の両方が来る（渋谷区の一覧は
    地方公共団体名の列に都名まで入っている）。揃えないと config.TARGET_WARDS と
    一致せず、**対象区の施設に「対象区の所管外。区境をまたぐ連携が前提」という
    逆の注記が付く**。提言の文面が変わるので表示だけの問題ではない。

    正規表現で「◯◯区」を拾う書き方はしない。「府中市」の府を都道府県と
    見なして「中市」になるような取り違えを作り込むだけなので、
    都内のデータであることを使って先頭の「東京都」だけを落とす。

    区名の列を持たない出典（文京区の施設一覧は住所しか無い）のために、
    住所も受ける。ここでも正規表現は使わず、**23 区の名前との前方一致**
    だけで判定する。一致しなければ元の値をそのまま返すので、
    市部の値（「府中市」）は壊れない。
    """
    # **`str(value or "")` にしてはいけない。** 欠測が float('nan') で来ると
    # nan は真なので `str(nan)` == "nan" が返り、区名として持ち回られる。
    # 実際に千代田区の一覧（区名の列が一部空）でこれが起き、
    # **公開中の提言 10 件のうち 3 件に「ただしnanの施設であり、対象区の所管外。
    # 区境をまたぐ連携が前提になる」と出ていた**——千代田区の施設なのに逆の注記。
    # この docstring が警告していた事故そのものを、欠測の側から踏んでいた。
    if value is None or (isinstance(value, float) and value != value):
        return ""
    s = str(value).strip()
    if s.lower() == "nan":
        return ""
    if s.startswith("東京都") and s != "東京都":
        s = s[len("東京都") :]
    for ward in TOKYO_23_WARDS:
        if s.startswith(ward):
            return ward
    return s


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
    """日本の官公庁 CSV を読む。文字コードは CP932 が多く UTF-8 も混在する。

    UTF-16 は **BOM がある場合だけ** 使う。候補の末尾に置いて総当たりすると、
    Python の utf-16 コーデックは BOM 無しでもリトルエンディアンとみなして
    偶数長のバイト列をほぼ何でも解読してしまい、CP932 のファイルを
    例外を出さずに文字化けした表として返す。判定不能なら止まるほうがよい。
    （新宿区の公共施設一覧が UTF-16。他区は CP932 か UTF-8。）
    """
    if path.open("rb").read(2) in (b"\xff\xfe", b"\xfe\xff"):
        return _drop_blank_rows(pd.read_csv(path, encoding="utf-16", **kwargs), path)
    for enc in ("utf-8-sig", "cp932", "utf-8", "euc_jp"):
        try:
            return _drop_blank_rows(pd.read_csv(path, encoding=enc, **kwargs), path)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "unknown", b"", 0, 1, f"{path.name} の文字コードを判定できない"
    )


def _drop_blank_rows(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    """全列が空の行を落とす。

    表計算から書き出した CSV は末尾に空行が付くことがある。値が無いので
    集計は変わらないが、**列の中身から列を特定する経路が壊れる**。
    `require_column` は「その列らしい値が何割あるか」で判定するため、
    空行が混じると一致率が下がって在るはずの列が見つからなくなる。

    実例: 文京区の区立図書館一覧は 18 行のうち 8 行が空で、経度が範囲内の
    行は 55.6% しかなかった。列名も中身も正しいのに「列 経度 は在るが
    中身が経度と合わない」で止まっていた。
    """
    if df.empty:
        return df
    blank = df.isna().all(axis=1)
    if not blank.any():
        return df
    print(f"[read] {path.name}: 全列が空の行を {int(blank.sum())} 行落とした")
    return df[~blank].reset_index(drop=True)


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


def _assume_geographic(gdf: gpd.GeoDataFrame, path: Path) -> gpd.GeoDataFrame:
    """座標系の定義が無いファイルを救う。

    国土数値情報 P13 都市公園（2011年版）の SHP には **.prj が入っていない**。
    そのままでは投影も距離計算もできない。国土数値情報は緯度経度（JGD2011）で
    配布されるので補えるが、**黙って決めつけない** —— 座標が日本の緯度経度の
    範囲に収まっていることを確かめ、何を仮定したかを表示する。
    """
    if gdf.crs is not None or not len(gdf):
        return gdf

    minx, miny, maxx, maxy = gdf.total_bounds
    if not (122 <= minx <= 154 and 20 <= miny <= 46 and maxx <= 154 and maxy <= 46):
        raise ValueError(
            f"{path.name} に座標系の定義（.prj）が無く、座標も日本の緯度経度に見えない: "
            f"bounds=({minx:.3f}, {miny:.3f}, {maxx:.3f}, {maxy:.3f})\n"
            "元の配布形式（測地系）を確認すること。"
        )
    print(
        f"[read] {path.name} に .prj が無い。座標が日本の緯度経度の範囲に収まるため "
        f"{CRS_GEOGRAPHIC} と仮定する",
        file=sys.stderr,
    )
    return gdf.set_crs(CRS_GEOGRAPHIC)


_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


def _repair_mojibake(gdf: gpd.GeoDataFrame, path: Path) -> gpd.GeoDataFrame:
    """Shift-JIS の DBF を latin-1 として読んだ文字化けを、**中身で判定して**直す。

    `.cpg` の無い SHP は GDAL が既定の文字コードで読むため、国土数値情報の
    ように DBF が CP932 のものは日本語が化ける。P13 都市公園がそれで、
    公園名が `\\x8bî\\x91ò...`（＝「駒沢オリンピック公園」）になっていた。
    **画面に出していなかったので 1 度も気付かれなかった**——今回、
    緑・公園の一覧を出すことにして初めて見えた。

    **ファイル名でも拡張子でも決め打たない**（CLAUDE.md「壊れ方の型」）。
    列の中身が次を全部満たすときだけ直す:

      1. いま日本語が 1 文字も入っていない（入っていれば既に正しい）
      2. 全ての値が latin-1 → CP932 で往復できる（1 つでも失敗したら触らない）
      3. 往復した結果、半数以上の値に日本語が現れる

    3 を「半数以上」にしてあるのは、英数字だけの列（コード・URL）が
    たまたま往復できてしまっても、日本語が出てこないので採らないため。
    """
    # **dtype で列を選ばない。** pandas の版によって文字列列の dtype は
    # `object` にも `str` にもなり、`== object` で絞ると新しい版で
    # **1 列も見ずに黙って通る**（実際そうなっていて、化けたまま出た）。
    # 中身に str が入っているかどうかだけで判定する。
    fixed: list[str] = []
    for c in [x for x in gdf.columns if x != gdf.geometry.name]:
        s = gdf[c]
        vals = [v for v in s.tolist() if isinstance(v, str) and v]
        if not vals or any(_JAPANESE.search(v) for v in vals):
            continue
        try:
            decoded = [v.encode("latin-1").decode("cp932") for v in vals]
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if sum(1 for v in decoded if _JAPANESE.search(v)) * 2 < len(decoded):
            continue
        gdf[c] = s.map(
            lambda v: v.encode("latin-1").decode("cp932") if isinstance(v, str) and v else v
        )
        fixed.append(c)

    if fixed:
        print(
            f"[read] {path.name}: DBF が CP932 と判定できたので "
            f"{len(fixed)} 列を復元（{', '.join(fixed)}）。例: {gdf[fixed[0]].iloc[0]}"
        )
    return gdf


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
        return _repair_mojibake(_assume_geographic(gpd.read_file(path), path), path)

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

    # 【罠】同じ内容が文字コード違いで 2 部入っていることがある。
    #
    # S12 駅別乗降客数は ZIP の中が UTF-8/ と Shift-JIS/ に分かれており、
    # 中身は同一。両方読むと全駅の乗降客数が 2 倍になる。
    # 上の市区町村別ファイルと違って**件数も列構成も完全に同じ**なので、
    # 結合後の件数を見ても気付けない。ファイル名で寄せて 1 つだけ読む。
    by_stem: dict[str, list[Path]] = {}
    for p in files:
        by_stem.setdefault(p.stem, []).append(p)
    if any(len(v) > 1 for v in by_stem.values()):
        # UTF-8 版があればそちらを採る（Shift-JIS は環境によって化ける）。
        picked = [
            next((q for q in v if "utf" in str(q.parent).lower()), v[0])
            for v in by_stem.values()
        ]
        print(
            f"[read] 同名のファイルが複数ある（文字コード違いの同一データ）。"
            f"{len(files)} → {len(picked)} ファイルに絞る（二重計上防止）: "
            f"{picked[0].relative_to(path)}"
        )
        files = sorted(picked)

    print(f"[read] {path} から {fmt} を {len(files)} ファイル読み込む")

    frames = []
    for p in files:
        try:
            g = gpd.read_file(p)
        except UnicodeDecodeError:
            g = gpd.read_file(p, encoding="cp932")
        if len(g):
            frames.append(_repair_mojibake(_assume_geographic(g, p), p))

    if not frames:
        raise ValueError(f"{path} 配下のファイルがすべて空だった")

    # 分割ファイルは列構成が揃っている前提だが、年度混在に備えて和集合で結合する。
    merged = pd.concat(frames, ignore_index=True)
    out = gpd.GeoDataFrame(merged, geometry="geometry", crs=frames[0].crs)
    if len(frames) > 1:
        print(f"[read] 結合後 {len(out):,} 件 / {len(out.columns)} 列")
    return out


def representative_points(gdf: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """点として扱うための代表点。

    P29 も P04 も実データは既に点なので、そのまま返す。
    緯度経度のまま centroid を取ると GEOS が警告を出す（球面での重心は
    正しくない）ため、面が来たときだけ投影してから重心を取る。
    """
    if (gdf.geom_type == "Point").all():
        return gdf.geometry
    return gdf.geometry.to_crs(CRS_PROJECTED).centroid.to_crs(CRS_GEOGRAPHIC)


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


def _pick_school_class_column(
    gdf: gpd.GeoDataFrame, path: Path, name_col: str
) -> str:
    """学校分類コードの列を、名称で裏を取りながら決める。

    P29 は列名が `P29_00x` しかなく、しかも **版によって意味が入れ替わる**。
    第1.1版は P29_003 が施設種別詳細（盲・聾・養護を別コードで持つ）、
    P29_004 が学校分類コード。第2.0版は P29_003 が学校分類コードで、
    P29_004 は名称。どちらの列も「16012 を含む 2〜30 種のコード列」に
    見えるため、中身の形だけでは選べない
    （第1.1版で P29_003 を選ぶと 67 件が 9 件に減る）。

    そこで名称から特別支援学校だと断定できる行を先に数え、
    **それを最も多く拾えるコード列**を選ぶ。取りこぼす方向の誤りは
    地図を見ても気付けないので、ここで別の手掛かりを当てておく。
    """
    named = gdf[name_col].astype(str).str.contains(SPECIAL_NEEDS_NAME_PATTERN)
    n_named = int(named.sum())
    if n_named == 0:
        raise ValueError(
            f"{path.name} の {name_col} に「特別支援学校」等を名称に持つ学校が 1 件も無い。\n"
            f"    {name_col} が学校名の列か確認すること: "
            f"{list(gdf[name_col].head(5))}\n"
            "対象範囲を絞った後のファイルなら、絞る前のファイルで実行すること。"
        )

    hits: list[tuple[str, int, int]] = []  # (列, 該当件数, 名称一致を拾えた件数)
    for col in gdf.columns:
        if col in ("geometry", name_col):
            continue
        codes = pd.to_numeric(gdf[col], errors="coerce")
        if codes.isna().mean() > 0.1 or not 2 <= int(codes.nunique()) <= 30:
            continue
        sel = codes == SCHOOL_CLASS_SPECIAL_NEEDS
        if not sel.any():
            continue
        hits.append((col, int(sel.sum()), int((sel & named).sum())))

    if not hits:
        raise ValueError(
            "\n".join(
                [
                    f"{path.name} から学校分類コードの列を特定できない。",
                    f"コード {SCHOOL_CLASS_SPECIAL_NEEDS}（特別支援学校）を含む列が"
                    "1 つも無かった。",
                    f"名称からは {n_named} 件が特別支援学校に見える。",
                    "",
                    "各列の実際の値:",
                    *_column_samples(gdf),
                    "",
                    "年度によってコード体系が違う可能性がある。"
                    "schema.SCHOOL_CLASS_SPECIAL_NEEDS を実データに合わせること。",
                ]
            )
        )

    col, n_sel, n_covered = max(hits, key=lambda h: (h[2], -h[1]))
    print(
        f"[p29] 学校分類コードの列 = {col}"
        f"（{SCHOOL_CLASS_SPECIAL_NEEDS} が {n_sel} 件 / "
        f"名称から特別支援学校と分かる {n_named} 件のうち {n_covered} 件を含む）"
    )
    for other, n_o, c_o in hits:
        if other != col:
            print(
                f"[p29] 　同じ形の列が他にもある: {other}"
                f"（{n_o} 件 / 名称一致 {c_o} 件）— 少ない方は採らない",
                file=sys.stderr,
            )

    # 取りこぼし: 名称で断定できるのに分類コードから漏れる。
    if n_covered < n_named:
        raise ValueError(
            f"{col} で絞ると、名称から特別支援学校と分かる {n_named} 件のうち "
            f"{n_named - n_covered} 件が漏れる:\n"
            f"    漏れた例: {list(gdf.loc[named & (pd.to_numeric(gdf[col], errors='coerce') != SCHOOL_CLASS_SPECIAL_NEEDS), name_col].head(5))}\n"
            "分類コードの列か SCHOOL_CLASS_SPECIAL_NEEDS が実データと合っていない。"
        )
    # 混入: 分類コードで拾った大半が特別支援学校らしくない
    #（学校名が「◯◯学園」の特別支援学校は実在するので全件一致は求めない）。
    if n_covered < n_sel * 0.5:
        raise ValueError(
            f"{col} で絞った {n_sel} 件のうち、名称から特別支援学校と分かるのは "
            f"{n_covered} 件しかない。分類コードの列を取り違えている疑いがある:\n"
            f"    絞り込まれた名称の例: "
            f"{list(gdf.loc[pd.to_numeric(gdf[col], errors='coerce') == SCHOOL_CLASS_SPECIAL_NEEDS, name_col].head(8))}"
        )
    return col


def _pick_school_founder_column(gdf: gpd.GeoDataFrame, name_col: str) -> str:
    """設置者コードの列を、名称で裏を取りながら決める。

    使い道は 1 つだけ——**在籍者数が一致しなかった学校を、
    「突き合わせに失敗した」と「元々その調査の対象外」に切り分ける。**
    都教委の調査は公立だけなので、国立・私立が漏れるのは正常だが、
    都立が 1 校でも漏れたら名寄せが壊れている。両者を区別できないと、
    名寄せの綻びが黙って既定値 150 人になる。

    列名 `P29_006` は他の 5 桁コード列と形が同じで、「小さな整数の列」では
    選べない。そこで**名称から作った 3 グループ**（区市町村立・大学附属・
    東京都立）が、その列で**3 つの別々の値にちょうど割れる**ことを条件にする。
    版がずれて別の意味の列を掴めば、この対応は成立せず止まる。
    """
    names = gdf[name_col].astype(str)
    groups = {
        label: names.str.contains(pattern, na=False)
        for label, pattern in SCHOOL_FOUNDER_NAME_PATTERNS.items()
    }
    empty = [label for label, mask in groups.items() if not mask.any()]
    if empty:
        raise ValueError(
            f"名称から設置者を推し量れるグループが空: {', '.join(empty)}。\n"
            f"    {name_col} が学校名の列か確認すること: {list(names.head(5))}\n"
            "schema.SCHOOL_FOUNDER_NAME_PATTERNS を実データに合わせること。"
        )

    for col in COLUMN_MAP["ksj_p29_school"]["founder"] + tuple(gdf.columns):
        if col not in gdf.columns or col in ("geometry", name_col):
            continue
        values = gdf[col].astype(str)
        if not 2 <= int(values.nunique()) <= 8:
            continue
        # 各グループが 1 つの値だけを取り、かつグループ同士で値が重ならないこと。
        codes = {}
        for label, mask in groups.items():
            taken = set(values[mask].unique())
            if len(taken) != 1:
                break
            codes[label] = taken.pop()
        else:
            if len(set(codes.values())) == len(codes):
                print(
                    f"[p29] 設置者コードの列 = {col}"
                    f"（{' / '.join(f'{v}={k}' for k, v in codes.items())}）"
                )
                return col

    raise ValueError(
        "\n".join(
            [
                "設置者コードの列を特定できない。",
                "名称から分かる 3 グループ"
                f"（{' / '.join(f'{k} {int(m.sum())}件' for k, m in groups.items())}）"
                "が、3 つの別々の値にきれいに割れる列が無かった。",
                "",
                "各列の実際の値:",
                *_column_samples(gdf),
                "",
                "設置者コードが無い版なら、在籍者数の突き合わせは使えない。"
                "公立の未一致を検出できなくなるため --assume-missing で通すこと。",
            ]
        )
    )


def read_school_enrollment(path: Path) -> dict[str, int]:
    """東京都教育委員会の在籍者数 CSV を「学校名 → 在籍者数」に畳む。

    **1 行が 1 校ではない。** 併置校は障害種別ごとに行が分かれ、
    同じ学校番号が 2 行に載る（光明学園 = 肢体 209 + 病弱 47）。
    1 行だけ採ると規模を 2〜3 割取りこぼす。S12 の駅（駅×事業者×路線）と
    同じ型なので、学校番号で束ねて合算する。

    最終行の「合計」は集計結果の検算に使ってから捨てる。**合算を
    忘れた／二重に数えた場合はここで合わないので黙って通らない。**
    """
    df = read_csv_japanese(path)
    id_col = require_column(
        df, path, "tokyo_sped_enrollment", "school_id",
        tag="sped-students", label="学校番号",
        why="併置校を 1 校に束ねる鍵。無いと障害種別ごとに別の学校として数える。",
    )
    name_col = require_column(
        df, path, "tokyo_sped_enrollment", "name",
        tag="sped-students", label="学校名",
        why="P29 の学校と突き合わせる鍵。",
        predicate=lambda s: s.astype(str).str.contains(
            SPECIAL_NEEDS_NAME_PATTERN + r"|学園", na=False
        ),
        # 合計行など学校名が空の行が混ざるため 100% にはならない。
        # 条件を緩めても誤って別の列を掴む余地は無い——設置者（"東京都"）も
        # 障害種別（"知的"）も、この語をひとつも含まない。
        min_ratio=0.6,
    )
    stu_col = require_column(
        df, path, "tokyo_sped_enrollment", "students",
        tag="sped-students", label="在籍者数（総数）",
        why=(
            "このレイヤーの規模そのもの。学部別の列を掴むと総数を大きく取りこぼす。\n"
            "Excel 版はヘッダが 6 段のセル結合なので、CSV 版を使うこと。"
        ),
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(1, 5000),
        # 設置者・障害種別・併置校のような区分の列を掴まないための下限。
        # 実データは 63 校で 60 以上の異なり値を持つ。
        whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 5,
    )

    ids = df[id_col].astype(str).str.strip()
    is_school = ids.str.fullmatch(r"\d+")
    students = pd.to_numeric(df[stu_col], errors="coerce")

    # 合計行（学校番号が数値でない行）を検算に使う。
    total_rows = students[~is_school].dropna()
    rows = df[is_school].assign(_n=students[is_school])
    require_nonempty(
        len(rows), tag="sped-students",
        what="学校番号を持つ行", why=f"{path.name} が在籍者数 CSV か確認すること。",
    )
    grouped = rows.groupby(ids[is_school]).agg(name=(name_col, "first"), n=("_n", "sum"))
    if len(total_rows):
        stated = float(total_rows.max())
        if abs(grouped["n"].sum() - stated) > 0.5:
            raise ValueError(
                f"{path.name} の合計が合わない: "
                f"学校番号で束ねた合計 {grouped['n'].sum():,.0f} 人 ≠ "
                f"ファイルの合計行 {stated:,.0f} 人。\n"
                "行の取りこぼしか二重計上がある。"
            )
        print(f"[sped-students] 合計 {stated:,.0f} 人（ファイルの合計行と一致）")

    table: dict[str, int] = {}
    for _, row in grouped.iterrows():
        key = normalize_school_name(row["name"])
        if key in table:
            raise ValueError(
                f"{path.name} で学校名が重複する: {row['name']!r}。"
                "接頭辞を外した名前が別の学校とぶつかっている。"
            )
        table[key] = int(row["n"])
    n_multi = int((rows.groupby(ids[is_school]).size() > 1).sum())
    print(
        f"[sped-students] {len(rows):,}行 → {len(table):,}校"
        f"（うち {n_multi} 校は障害種別で行が分かれており合算した）"
    )
    return table


def _match_school_students(
    gdf: gpd.GeoDataFrame, name_col: str, students_path: Path
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """各校に在籍者数を当て、規模不明の行を区別できる形で返す。

    出典が 3 つに分かれる。**公立以外が漏れるのは正常だが、公立が漏れたら
    名寄せが壊れている**——両者を設置者コードで切り分け、後者では止まる。
    名寄せの綻びを既定値 150 人として飲み込むと、規模が消えたことに
    出力を眺めても気付けない（そのために A4 が長く残っていた）。

      都立・区立   東京都教育委員会の在籍者数 CSV。1 校でも漏れたら止まる
      私立         各校の自己公表値（SCHOOL_STUDENTS_SELF_REPORTED）
      国立         公表が無い。既定値のまま students_assumed を立てる

    返すのは (在籍者数, 既定値で埋めたか, 規模の出典) の 3 本。
    """
    table = read_school_enrollment(students_path)
    founder_col = _pick_school_founder_column(gdf, name_col)
    founder = gdf[founder_col].astype(str)
    public_codes = {
        founder[gdf[name_col].astype(str).str.contains(pattern, na=False)].iloc[0]
        for label, pattern in SCHOOL_FOUNDER_NAME_PATTERNS.items()
        if label in ("市区町村立", "都道府県立")
    }

    students = pd.Series(float("nan"), index=gdf.index)
    origin = pd.Series("", index=gdf.index)
    unmatched_public: list[str] = []
    unknown: list[str] = []
    for idx, raw in gdf[name_col].astype(str).items():
        key = normalize_school_name(raw)
        if key in table:
            students[idx] = table[key]
            origin[idx] = SOURCES["tokyo_sped_enrollment"].label
        elif key in SCHOOL_STUDENTS_SELF_REPORTED:
            n, as_of, _url = SCHOOL_STUDENTS_SELF_REPORTED[key]
            students[idx] = n
            origin[idx] = f"{raw} 公表値（{as_of}）"
        elif founder[idx] in public_codes:
            unmatched_public.append(raw)
        else:
            unknown.append(raw)
            students[idx] = SCHOOL_STUDENTS_FALLBACK
            origin[idx] = SCHOOL_STUDENTS_UNKNOWN_LABEL

    if unmatched_public:
        raise ValueError(
            "\n".join(
                [
                    f"公立の {len(unmatched_public)} 校が在籍者数 CSV と一致しない:",
                    *(f"    {n}" for n in unmatched_public[:10]),
                    "",
                    "公立は全件が調査対象なので、一致しないのは名寄せが壊れている"
                    "か、年度がずれて統廃合された学校がある。",
                    f"CSV 側の学校名の例: {list(table)[:8]}",
                    "",
                    "schema.normalize_school_name を実データに合わせること。"
                    "既定値で埋めて通すと、規模が消えたことに気付けない。",
                ]
            )
        )

    assumed = students.isna() | origin.eq(SCHOOL_STUDENTS_UNKNOWN_LABEL)
    n_real = int((~assumed).sum())
    print(
        f"[p29] 在籍者数: 実数 {n_real}/{len(gdf)} 校"
        f"（計 {students[~assumed].sum():,.0f} 人）"
    )
    if unknown:
        print(
            f"[p29] 在籍者数が公表されていない {len(unknown)} 校は"
            f"既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人（規模不明として区別する）: "
            f"{', '.join(unknown)}",
            file=sys.stderr,
        )
    return students.fillna(SCHOOL_STUDENTS_FALLBACK), assumed, origin


def normalize_schools(
    path: Path, students_path: Path | None = None, assume_missing: bool = False
) -> gpd.GeoDataFrame:
    """国土数値情報 P29 から特別支援学校を抽出する。

    学校分類コードの列を取り違えると、**全学校が特別支援学校として**
    需要に乗る（小中高を含めれば件数は数十倍になる）。逆に 1 つずらすと
    静かに取りこぼす。生徒数の列を取り違えると、全件が既定値 150 人になり
    「規模で重み付けする」という前提そのものが消える。どれも黙って通さない。

    絞り込みは **研究領域で切る前**に行う。名称による裏取り（都全体で
    48 件）は母数が大きいほど効くうえ、対象 2 区に絞ってからでは
    数件しか残らず「列を取り違えた」のか「元々少ない」のか区別できない。

    **P29 は在籍者数を持たない**（第1.1版・第2.0版のいずれにも無い）。
    規模は別の出典から当てる——公立は東京都教育委員会の在籍者数 CSV
    （`students_path`）、私立は各校の自己公表値
    （`schema.SCHOOL_STUDENTS_SELF_REPORTED`）。国立 3 校はどこも
    在籍者数を公表していないため既定値のままで、`students_assumed` が立つ。
    """
    gdf = read_vector(path).to_crs(CRS_GEOGRAPHIC)
    total = len(gdf)

    # 名称の列を先に決める。分類コード列の裏取りに使うため、
    # ここが外れると以降がすべて無意味になる（optional ではない）。
    name_col = require_column(
        gdf,
        path,
        "ksj_p29_school",
        "name",
        tag="p29",
        label="学校名",
        why=(
            "学校名から特別支援学校を数え、分類コードの列を裏取りする。"
            "特定できないまま進めると、分類コード列の取り違えを検出できない。"
        ),
        # 学校コード（A113210200158）も所在地もほぼ一意なので、
        # 「一意な文字列」だけでは選べない。学校名らしい語を条件にする。
        predicate=lambda s: s.astype(str).str.contains(SCHOOL_NAME_PATTERN, na=False),
        min_ratio=0.8,
        whole=lambda s: int(s.nunique()) >= max(3, int(len(s) * 0.5)),
        name_hint=r"名称|学校名",
    )

    cls = _pick_school_class_column(gdf, path, name_col)
    codes = pd.to_numeric(gdf[cls], errors="coerce")
    gdf = gdf[codes == SCHOOL_CLASS_SPECIAL_NEEDS].copy()
    print(f"[p29] {total:,}件 → 特別支援学校 {len(gdf):,}件（{cls} で判定）")

    gdf = clip_to_study_area(gdf)
    require_nonempty(
        len(gdf),
        tag="p29",
        what="研究領域内の特別支援学校",
        why="対象範囲か入力を確認すること。",
        per_file=True,
    )
    print(f"[p29] 研究領域内 {len(gdf):,}件")

    stu_col = resolve_column(
        gdf,
        "ksj_p29_school",
        "students",
        tag="p29",
        label="児童生徒数",
        # 児童生徒数は「ただの正の整数」で、建築年や座標系コードと
        # 値域が重なる。列名の手掛かりが無ければ推定しない。
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(1, 5000),
        whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 5,
        name_hint=r"児童|生徒|人数|在籍",
        require_hint=True,
    )
    if stu_col is not None:
        # 将来の版が在籍者数を持つようになった場合。ファイル内にあるなら
        # それが最も揃った出典なので、外部の突き合わせより優先する。
        students = pd.to_numeric(gdf[stu_col], errors="coerce")
        assumed = students.isna()
        if assumed.any():
            print(
                f"[p29] {stu_col} が空の {int(assumed.sum()):,}/{len(gdf):,}件は"
                f"既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人で補う",
                file=sys.stderr,
            )
        students = students.fillna(SCHOOL_STUDENTS_FALLBACK)
        origin = pd.Series(SOURCES["ksj_p29_school"].label, index=gdf.index)
        origin[assumed] = SCHOOL_STUDENTS_UNKNOWN_LABEL
    elif students_path is not None:
        students, assumed, origin = _match_school_students(gdf, name_col, students_path)
    elif assume_missing:
        print(
            f"[p29] 在籍者数の出典が無い。全件を既定値 {SCHOOL_STUDENTS_FALLBACK:.0f} 人"
            "として扱う（--assume-missing）。規模による重み付けは効かない",
            file=sys.stderr,
        )
        students = pd.Series(SCHOOL_STUDENTS_FALLBACK, index=gdf.index)
        assumed = pd.Series(True, index=gdf.index)
        origin = pd.Series(SCHOOL_STUDENTS_UNKNOWN_LABEL, index=gdf.index)
    else:
        # 他の「列を特定できない」ガードと同じ ValueError にそろえる。
        # SystemExit は Exception を継承しないため、検査側が捕まえられない。
        raise ValueError(
            "\n".join(
                [
                    "[p29] 在籍者数の出典が指定されていない。",
                    "P29 は第1.1版・第2.0版のどちらにも在籍者数を持たない。"
                    "規模で重み付けするには外から当てる必要がある。",
                    "",
                    "東京都教育委員会 公立学校統計調査報告書【東京都公立学校一覧】の",
                    "「特別支援学校（学校別在籍者数）」CSV を --students に渡すこと:",
                    "  https://www.kyoiku.metro.tokyo.lg.jp/about/statistics_and_research"
                    "/list_of_public_school/school_lists2025/report2025_csv",
                    "",
                    "  python -m etl.fetch --normalize p29 <P29> "
                    "--students <在籍者数CSV>",
                    "",
                    f"規模を捨てて全件 {SCHOOL_STUDENTS_FALLBACK:.0f} 人で通すなら "
                    "--assume-missing。ただしこのレイヤーは"
                    "「近くに特別支援学校があるか」というフラグになる。",
                ]
            )
        )

    out = gpd.GeoDataFrame(
        {
            "name": gdf[name_col] if name_col else "特別支援学校",
            "kind": "特別支援学校",
            "capacity": students,
            "weight": 1.0,
            "demand_value": students,
            "source": origin,
            "students_assumed": assumed,
            "synthetic": False,
        },
        geometry=representative_points(gdf),
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
            # P14 は定員フィールドを持たないので全件が仮定員。
            # 列名は WAM NET 側と揃える——同じ意味の列が出典ごとに別名だと、
            # 「仮定員の行だけ落とす」検査が片方の出典で静かに空振りする。
            "capacity_assumed": True,
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
        why=f"{len(df):,}件を読んだが対象区に入るものが無い。",
        per_file=True,
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
        assumed = pd.Series(True, index=gdf.index)
    else:
        capacity = pd.to_numeric(gdf[cap_col], errors="coerce")
        # **どの行が仮定員かを列として残す。** 後から
        # 「capacity == assumed_capacity(kind)」で復元しようとすると、
        # 実定員がたまたま仮定員と同じ値だった行（生活介護の定員 20 人など）を
        # 仮定と誤判定する。需要寄与の 68.9% がこの側から来ている以上、
        # その 68.9% 自体が推定値では感度分析の土台にならない。
        assumed = capacity.isna()
        blank = int(assumed.sum())
        if blank:
            # 訪問系・相談系・居住系には制度上そもそも定員が無く、WAM NET でも
            # 空欄になる。一律の既定値で埋めると「人が集まらない拠点」に
            # 通所系と同じ規模を与えるので、種別ごとの仮定員へ寄せる。
            print(
                f"[wamnet] {cap_col} が空の {blank:,}/{len(gdf):,}件は"
                "種別ごとの仮定員で補う（訪問系・相談系は制度上定員なし）",
                file=sys.stderr,
            )
        capacity = capacity.fillna(kinds.map(assumed_capacity))
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
            "capacity_assumed": assumed,
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

    # 診療科目欄は「内科　小児科　皮膚科」のように連結されている。
    # 中身から探すときは、よくある診療科名を含む列を目印にする。
    # 略称だけの行（「歯　小歯　矯歯」）も拾えるよう略称も混ぜる。
    dep_hint = "|".join(
        ("内科", "外科", "小児科", "皮膚科", "眼科", "耳鼻", "産婦人科", "歯科", "歯")
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

    deps = gdf[dep_col].astype(str)
    hit = deps.map(is_psych_clinic)
    gdf = gdf[hit]
    require_nonempty(
        len(gdf),
        tag="p04",
        what=f"精神科・心療内科（{dep_col} が {'/'.join(PSYCH_CLINIC_KEYWORDS)} "
        f"または略称 {'/'.join(sorted(PSYCH_CLINIC_ABBREV))} を含む行）",
        why=(
            f"研究領域内 {total:,}件のいずれも該当しない。"
            f"{dep_col} が診療科目の列か確認すること: "
            f"python -m etl.fetch --inspect {path}"
        ),
    )
    # 略称でしか書かれていない医療機関がどれだけ居たかを出す。
    # 正式名称だけで絞っていた頃は、この分を静かに取りこぼしていた。
    by_full = int(deps[hit].str.contains("|".join(PSYCH_CLINIC_KEYWORDS)).sum())
    print(
        f"[p04] {total:,}件 → 精神科・心療内科 {len(gdf):,}件（{dep_col} で判定）\n"
        f"       うち正式名称 {by_full:,}件 / 略称のみ {len(gdf) - by_full:,}件"
    )

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
        geometry=representative_points(gdf),
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


def normalize_population(path: Path) -> pd.DataFrame:
    """e-Stat 経済センサスの地域メッシュ統計から、昼間その場所に居る人の量を作る。

    **昼間人口そのもののメッシュ統計は配信されていない。** 国勢調査の地域メッシュ
    統計は人口等基本集計（常住地＝夜間人口）と就業状態等基本集計までで、
    従業地・通学地集計はメッシュ単位で公表されていない。そこで
    **経済センサスの従業者数**（500m メッシュ）を使う。

    「その場所で働いている人の数」であって昼間人口ではない。自宅にいる人も
    買い物客も含まない。ただし本作が測りたいのは人的密度が生む
    視覚・聴覚の多重刺激であり、住宅で在宅している人は含まない方が近い。
    過小・過大の両方向を含むことは methodology に明記する。

    列名は **事業所数と従業者数で完全に同一**（どちらも「Ａ～Ｓ全産業」）。
    列名で選ぶことが原理的にできないため、男女別の内訳と一致する方を採る。
    """
    from . import mesh as meshlib

    raw = read_csv_japanese(path, dtype=str)
    if len(raw) < 2:
        raise ValueError(f"{path.name} に十分な行が無い（{len(raw)} 行）")

    # 1 行目が列 ID、2 行目が日本語のラベル、3 行目からデータ。
    labels = {c: str(raw.iloc[0][c]).strip().strip("　") for c in raw.columns}
    df = raw.iloc[1:].reset_index(drop=True)

    code_col = pick_column(df, ("KEY_CODE", "key_code", "メッシュコード"))
    if code_col is None:
        code_col = df.columns[0]
        print(f"[estat] メッシュコードの列は先頭列 {code_col} とみなす", file=sys.stderr)

    total_label = "Ａ～Ｓ全産業"
    cands = [c for c, v in labels.items() if v == total_label]
    if not cands:
        raise ValueError(
            "\n".join(
                [
                    f"{path.name} に「{total_label}」の列が無い。",
                    "経済センサスの産業別集計（事業所数・従業者数）を想定している。",
                    "",
                    "各列のラベル:",
                    *(f"    {c}: {v}" for c, v in labels.items()),
                ]
            )
        )

    num = df[cands].apply(pd.to_numeric, errors="coerce")
    # 【罠】事業所数と従業者数はラベルが 1 文字も違わない。
    # 事業所数（渋谷駅の 500m メッシュで 641）を人的密度として使うと、
    # **小さな店が並ぶ通りと大企業の本社ビルが同じ重さ**になる。
    # 男女別の内訳が別列にあるので、その和と一致する列を従業者数とみなす。
    male = next((c for c, v in labels.items() if v == f"男-{total_label}"), None)
    female = next((c for c, v in labels.items() if v == f"女-{total_label}"), None)
    col = None
    if male and female:
        mf = pd.to_numeric(df[male], errors="coerce") + pd.to_numeric(
            df[female], errors="coerce"
        )
        # 男女の和は総数と完全一致しない（性別不詳がある）。
        # 事業所数とは桁が違うので、相対 5% で十分に見分けられる。
        for c in cands:
            agree = float(
                ((num[c] - mf).abs() / num[c].clip(lower=1)).le(0.05).mean()
            )
            if agree > 0.95:
                col = c
                print(
                    f"[estat] 従業者数の列 = {c}"
                    f"（男女別の和と {agree * 100:.1f}% のメッシュで一致）"
                )
                break
    if col is None:
        col = max(cands, key=lambda c: float(num[c].sum()))
        print(
            f"[estat] 男女別の内訳で確かめられないため、合計が最大の列 {col} を"
            f"従業者数とみなす（候補 {cands}）",
            file=sys.stderr,
        )
    if len(cands) > 1:
        others = {c: int(num[c].sum()) for c in cands if c != col}
        print(f"[estat] 　同じラベルの列（事業所数と見なす）: {others}", file=sys.stderr)

    out = pd.DataFrame(
        {
            "mesh_code": df[code_col].astype(str).str.strip(),
            "daytime_population": num[col].to_numpy(),
        }
    ).dropna()
    require_nonempty(
        len(out),
        tag="estat",
        what="従業者数を読めたメッシュ",
        why=f"{len(df):,}行を読んだが数値にならない。実際の値: {list(df[col].head(5))}",
    )

    # 研究領域の外は捨てる。他のレイヤーと同じく余白付きの矩形で切る。
    minx, miny, maxx, maxy = study_bbox()
    inside = []
    for code in out["mesh_code"]:
        try:
            cell = meshlib.decode(code)
        except Exception:  # noqa: BLE001 — 桁数違いの行は落とす
            inside.append(False)
            continue
        lon, lat = cell.center
        inside.append(minx <= lon <= maxx and miny <= lat <= maxy)
    out = out[pd.Series(inside, index=out.index)].reset_index(drop=True)
    require_nonempty(
        len(out),
        tag="estat",
        what="研究領域内のメッシュ",
        why="メッシュコードの桁数か対象範囲を確認すること。",
        per_file=True,
    )

    level = len(out["mesh_code"].iloc[0])
    print(
        f"[estat] {len(df):,}メッシュ → 研究領域内 {len(out):,}メッシュ"
        f"（{level}桁 = {meshlib.LEVEL_LABEL[next(lv for lv, n in meshlib.CODE_LENGTH.items() if n == level)]}）/ "
        f"従業者 {out['daytime_population'].sum():,.0f}人"
    )
    return out


def normalize_stations(path: Path) -> gpd.GeoDataFrame:
    """国土数値情報 S12 駅別乗降客数を点データにする。

    S12 は **1 行が「駅 × 事業者 × 路線」** で、渋谷駅は JR・東急・メトロ・京王の
    4 行に分かれる。1 行だけ採ると（東急 177 万人）JR の 65 万人が消え、
    最大 3 分の 1 を捨てることになる。グループコードで束ねて合算する。

    合算しても二重計上にならないのは、同じ数字を複数行に持つ場合に
    **重複コードが立ち、片方の乗降客数が 0 になっている**ため
    （渋谷の東急は東横線に 177 万人、田園都市線・半蔵門線・副都心線は 0）。

    乗降客数は 2011〜2024 年の 14 年分が横に並ぶ。列を 1 つ間違えると
    10 年以上前の数字で計算しても何も起きない。最新年の列を選び、
    **どの列を選び、その結果どの駅が最大になったか**を必ず表示する。
    """
    gdf = clip_to_study_area(read_vector(path).to_crs(CRS_GEOGRAPHIC))
    total = len(gdf)
    require_nonempty(
        total,
        tag="s12",
        what="研究領域内の駅",
        why="全国ファイルか、対象範囲を確認すること。",
    )

    name_col = require_column(
        gdf,
        path,
        "ksj_s12_station",
        "name",
        tag="s12",
        label="駅名",
        why="根拠カードと提言文に出る。路線名や事業者名を掴むと駅名にならない。",
        # 駅名・事業者名・路線名がすべて文字列で並ぶ。事業者名（181 種）と
        # 路線名（561 種）は駅名（8,747 種）より桁違いに種類が少ない。
        predicate=lambda s: s.astype(str).str.len().between(1, 20),
        whole=lambda s: int(s.nunique()) >= max(3, int(len(s) * 0.5)),
        name_hint=r"駅名|station",
    )
    # 乗降客数。年ごとに 14 列並ぶので、**最後に現れるもの＝最新年**を採る。
    pax_col = require_column(
        gdf,
        path,
        "ksj_s12_station",
        "passengers",
        tag="s12",
        label="乗降客数",
        why=(
            "駅の過負荷を測る唯一の量。特定できないまま進めると"
            "需要側の主軸が消える。"
        ),
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(0, 3_000_000),
        whole=lambda s: int(pd.to_numeric(s, errors="coerce").nunique()) >= 5
        and float(pd.to_numeric(s, errors="coerce").max()) > 10_000,
        name_hint=r"乗降|passenger",
    )
    group_col = require_column(
        gdf,
        path,
        "ksj_s12_station",
        "group",
        tag="s12",
        label="グループコード",
        why=(
            "同じ駅の事業者別の行を束ねるのに使う。特定できないまま進めると"
            "渋谷駅が 4 つの別々の駅として扱われ、それぞれの乗降客数も部分値になる。"
        ),
        # 同じコードの行が地理的に固まっていること（同じ駅なのだから）。
        # 駅コードは 1 行 1 コードで束ねられず、路線名は離れた駅を束ねてしまう。
        predicate=lambda s: s.astype(str).str.fullmatch(r"\d{4,8}").fillna(False),
        whole=lambda s: _groups_are_compact(gdf, s),
        name_hint=r"group|グループ",
    )

    pax = pd.to_numeric(gdf[pax_col], errors="coerce").fillna(0.0)
    gdf = gdf.assign(_pax=pax, _group=gdf[group_col].astype(str))

    # 駅の位置は路線ごとのホーム（線分）。束ねた駅の代表点は
    # 乗降客数の大きいホームの中点を採る（改札の重心に最も近い）。
    metric = gdf.to_crs(CRS_PROJECTED)
    gdf = gdf.assign(_pt=metric.geometry.interpolate(0.5, normalized=True).to_numpy())

    rows = []
    for code, grp in gdf.groupby("_group"):
        lead = grp.loc[grp["_pax"].idxmax()]
        rows.append(
            {
                "name": str(lead[name_col]),
                "kind": "駅",
                "capacity": float(grp["_pax"].sum()),
                "weight": 1.0,
                "demand_value": float(grp["_pax"].sum()),
                "source": SOURCES["ksj_s12_station"].label,
                "synthetic": False,
                "geometry": lead["_pt"],
            }
        )

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_PROJECTED).to_crs(
        CRS_GEOGRAPHIC
    )
    # 乗降客数が 0 の駅（貨物駅・未集計）は需要を生まないので落とす。
    dropped = int((out["capacity"] <= 0).sum())
    out = out[out["capacity"] > 0].copy()
    require_nonempty(
        len(out),
        tag="s12",
        what=f"乗降客数が入っている駅（{pax_col}）",
        why=f"研究領域内 {total:,}行のいずれも 0 だった。年の列を確認すること。",
    )
    out["lon"] = out.geometry.x
    out["lat"] = out.geometry.y

    print(
        f"[s12] {total:,}行（駅×事業者×路線）→ {len(out):,}駅"
        f"{f'（乗降客数 0 の {dropped} 駅は除外）' if dropped else ''}"
    )
    print(f"[s12] 乗降客数の列 = {pax_col} / 上位 5 駅:")
    for _, r in out.nlargest(5, "capacity").iterrows():
        print(f"        {r['name']:<10} {r['capacity']:>10,.0f} 人/日")
    return out.reset_index(drop=True)


def _groups_are_compact(gdf: gpd.GeoDataFrame, codes: pd.Series) -> bool:
    """同じコードを持つ行が同じ駅と言える距離に収まっているか。

    「同じ駅の別事業者」を束ねる列を、中身から見分けるための条件。
    路線名や事業者名で束ねると、離れた駅どうしが 1 つになる。
    """
    metric = gdf.to_crs(CRS_PROJECTED).geometry
    n_multi = 0
    for _, idx in codes.groupby(codes).groups.items():
        if len(idx) < 2:
            continue
        n_multi += 1
        pts = metric.loc[idx]
        if pts.distance(pts.iloc[0]).max() > 1500.0:
            return False
    # 束ねる先が 1 つも無い列（全行が別コード）は、束ねる役に立たない。
    return n_multi > 0


def normalize_parks(path: Path, clip: bool = True) -> gpd.GeoDataFrame:
    """国土数値情報 P13 都市公園を被覆ポリゴンにする。

    **P13 は点データで、公園の形は入っていない。** 面積が属性として入るだけで、
    「メッシュ面積に占める公園の割合」を出すには形が要る。
    そこで各公園を **同じ面積の円** に置き換える。代々木公園（54ha）なら
    半径 415m の円になる。細長い河川緑地などは形が実物と違うが、
    メッシュ（250m 角）より小さい公園が大半で、面積の総量は保存される。

    面積の列は「ただの正の整数」で、供用開始年（1873〜2011）と値域が重なる。
    年として解釈できない大きな値を含むことを条件にして選ぶ。
    """
    gdf = read_vector(path).to_crs(CRS_GEOGRAPHIC)
    total = len(gdf)

    area_col = require_column(
        gdf,
        path,
        "ksj_p13_park",
        "area",
        tag="p13",
        label="公園面積",
        why=(
            "点データを同じ面積の円に置き換えて被覆率を出す。"
            "特定できないまま進めると公園の大きさが区別できず、"
            "街区公園も都立公園も同じ広さとして減点に効く。"
        ),
        predicate=lambda s: pd.to_numeric(s, errors="coerce").between(1, 5_000_000),
        # 供用開始年と区別する条件。年として有り得ない大きさの値が
        # 含まれること（都立公園は 10 万 m² 級）。
        whole=lambda s: (
            float(pd.to_numeric(s, errors="coerce").max()) > 3000
            and int(pd.to_numeric(s, errors="coerce").nunique()) >= 20
        ),
        name_hint=r"面積|area",
    )
    name_col = optional_column(
        gdf,
        "ksj_p13_park",
        "name",
        tag="p13",
        label="公園名",
        fallback="根拠カードに公園名が出ない",
    )

    if clip:
        gdf = clip_to_study_area(gdf)
    require_nonempty(
        len(gdf),
        tag="p13",
        what="研究領域内の都市公園",
        why=f"{total:,}件を読んだが対象区に入るものが無い。",
        per_file=True,
    )

    area = pd.to_numeric(gdf[area_col], errors="coerce")
    blank = int(area.isna().sum())
    if blank:
        print(
            f"[p13] {area_col} が空の {blank:,}/{len(gdf):,}件は被覆率に数えない",
            file=sys.stderr,
        )
    gdf = gdf[area.notna()].copy()
    area = area[area.notna()]

    metric = gdf.to_crs(CRS_PROJECTED)
    if (metric.geom_type == "Point").all():
        # 面積が等しい円へ。r = √(A/π)
        radius = np.sqrt(area.to_numpy() / math.pi)
        geometry = metric.geometry.buffer(radius)
        print(f"[p13] 点データのため面積の等しい円に変換（半径 "
              f"{radius.min():.0f}〜{radius.max():.0f}m）")
    else:
        geometry = metric.geometry

    out = gpd.GeoDataFrame(
        {
            "name": (
                gdf[name_col].astype(str).to_numpy() if name_col else ""
            ),
            "area_m2": area.to_numpy(),
            "source": SOURCES["ksj_p13_park"].label,
            "synthetic": False,
        },
        geometry=geometry.to_numpy(),
        crs=CRS_PROJECTED,
    ).to_crs(CRS_GEOGRAPHIC)

    print(
        f"[p13] {total:,}件 → 研究領域内 {len(out):,}件 / "
        f"面積 {area.min():,.0f}〜{area.max():,.0f}m²（合計 {area.sum() / 1e6:.2f}km²）"
    )
    return out.reset_index(drop=True)


# 常時監視の表を見分けるための、中身の手掛かり。
#
# **年度フォルダには常時監視と要請限度が並んで入っている。** どちらも
# 「幹線道路の道路端で測った昼間 LAeq」だが、**測定点の選ばれ方が違う**:
# 常時監視は騒音規制法第18条の常時監視で、幹線道路を年度ごとに
# ローテーションして系統的に測る。要請限度は苦情の出た道路を測る調査で、
# 平均が 1.3dB 高く、区ごとの点数も偏る（文京区・渋谷区は 0 点、練馬区は 48 点）。
# **混ぜると `docs/issues.md` A1（測定点の選ばれ方が区に依存する）を
# そのまま持ち込む**ので、常時監視だけを採る。
#
# **ファイル名でもシート名でも判定しない。** 手元に届くファイル名は
# `cyousakekka$300500a20210401153420268.files$2019monitoring.csv` のように
# ブラウザが化けさせた形で、`monitoring` が入っている年と入っていない年がある。
# 中身で判定する。手掛かりは 2 つで、どちらも 5 年分すべてで確認した:
#
#   1. **環境基準の地域類型が大文字 A/AA/B/C**。要請限度の「区域の区分」は
#      小文字 a/b/c で入る。これは表記のゆれではなく、環境基本法の地域類型と
#      騒音規制法の区域区分という**別の法概念**の書き分けである。
#   2. **遮音壁等の有無・低騒音舗装の有無（○/×）の列がある**。
#      要請限度の表には無い。
#
# 1 だけだと判定が 1 文字の大小に乗るので、2 つとも要求する。
# どちらが欠けたのかは例外に書く。
_AREA_TYPE_VALUES = ("A", "AA", "B", "C")
_YESNO_MARKS = ("○", "〇", "◯", "×", "✕")
# 要請限度の「区域の区分」は**小文字** a/b/c。表記のゆれではなく、
# 環境基本法の地域類型（大文字）と騒音規制法の区域区分（小文字）という
# **別の法概念の書き分け**である（`docs/status.md`）。
_ZONE_DIVISION_VALUES = ("a", "b", "c")

# 調査の呼び名。**この 2 つを画面まで運ぶ。** 測定点の選ばれ方が違うので、
# 同じ層に入れても「どちらの調査で測った点か」は最後まで区別できなければ
# ならない（`docs/issues.md` A1）。
SURVEY_MONITORING = "常時監視"
SURVEY_REQUEST_LIMIT = "要請限度"

# 位置参照情報が配る緯度経度の桁数。同じ街区なら完全に同じ値が返るので、
# この桁で束ねれば「同じ地点の別年度」だけがまとまる。
_ISJ_DECIMALS = 6


def _clean_label(value: object) -> str:
    """見出しのセルを 1 語にする。全角空白と改行を落として NFKC で揃える。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", "", text)


def _flatten_header(raw: pd.DataFrame) -> pd.DataFrame | None:
    """複数行にまたがる見出しを 1 行にたたむ。たためなければ None。

    **都環境局の調査結果は見出しが 2〜3 行ある。** 常時監視は
    「等価騒音レベル(dB)」が 1 行目、「昼間 / 夜間」が 3 行目にあり、
    1 行目だけを見出しとして読むと**昼夜の区別が列名から消える**。
    そうなると `require_column` の `name_hint=r"昼"` が効かず、
    値域だけで選ぶことになって夜間を掴み得る（実測で 3〜4dB 低い）。

    見出しが何行あるかは**行の中身で決める**。データ行は数値のセルを
    複数持ち、見出し行は持たない（単位だけの行 `(m) (m) (m)` も
    数値ではないので見出し側に入る）。行数で決め打ちすると、
    年度ごとに違う様式で静かにずれる。
    """
    numeric = [
        int(pd.to_numeric(pd.Series(list(raw.iloc[i])), errors="coerce").notna().sum())
        for i in range(min(len(raw), 12))
    ]
    start = next((i for i, n in enumerate(numeric) if n >= 2), None)
    if not start:  # 0（見出しが無い）と None（データ行が無い）はどちらも扱えない
        return None

    header, body = raw.iloc[:start], raw.iloc[start:].reset_index(drop=True)
    names: list[str] = []
    seen: dict[str, int] = {}
    for i, col in enumerate(raw.columns):
        parts: list[str] = []
        for cell in header[col]:
            text = _clean_label(cell)
            if text and text not in parts:
                parts.append(text)
        # 見出しの空欄は上の行から埋めない。埋めると「車道端からの距離」に
        # 隣の「評価対象道路②」が乗るなど、無関係な語が混ざる。
        # 埋めなくても昼夜は区別できる（夜間の列は見出しが「夜間」だけになる）。
        name = "".join(parts) or f"列{i + 1}"
        seen[name] = seen.get(name, 0) + 1
        # 「車線数」「道路種別」は評価対象道路①と②で 2 回出る。
        # 重複したままだと df[col] が DataFrame を返し、列の中身を見る
        # 経路がまとめて壊れる。
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    body.columns = names
    return body


def _read_noise_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """騒音調査結果のファイルから、表になりそうなものを全部読む。

    Excel は 1 冊に「項目説明」「常時監視測定地点」「要請限度測定地点」が
    入っている。**どれを読むかはシート名で決めない**（年度ごとに
    「常時監視地点別測定結果(R01年度)」「常時監視測定地点（R02年度）」と揺れる）。
    全部読んで `_pick_monitoring_table` が中身で選ぶ。
    """
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        book = pd.ExcelFile(path)
        raws = [
            (sheet, book.parse(sheet, header=None, dtype=object))
            for sheet in book.sheet_names
        ]
    else:
        raws = [(path.name, read_csv_japanese(path, header=None, dtype=str))]

    tables: list[tuple[str, pd.DataFrame]] = []
    for name, raw in raws:
        flat = _flatten_header(raw)
        if flat is not None and len(flat):
            tables.append((name, flat))
    return tables


def _monitoring_markers(df: pd.DataFrame) -> tuple[str | None, list[str]]:
    """(地域類型の列, ○×の列) を中身から探す。見つからなければ None / 空。"""

    def cleaned(s: pd.Series) -> pd.Series:
        return s.dropna().astype(str).map(_clean_label)

    area_type = None
    marks: list[str] = []
    for col in df.columns:
        values = cleaned(df[col])
        if len(values) < 5:
            continue
        # 大文字と小文字を区別する。ここが常時監視と要請限度の分かれ目。
        if area_type is None and values.isin(_AREA_TYPE_VALUES).mean() >= 0.9:
            area_type = str(col)
        if values.isin(_YESNO_MARKS).mean() >= 0.9:
            marks.append(str(col))
    return area_type, marks


def _request_limit_marker(df: pd.DataFrame) -> str | None:
    """要請限度の「区域の区分」列（**小文字** a/b/c）を中身から探す。

    **常時監視の否定として判定しない。** 「大文字 A/B/C が無い表 = 要請限度」に
    すると、様式が変わって地域類型の列が落ちた常時監視の表を要請限度として
    読むことになる。**どちらも積極的な手掛かりで判定する。**
    """
    for col in df.columns:
        values = df[col].dropna().astype(str).map(_clean_label)
        if len(values) < 5:
            continue
        if values.isin(_ZONE_DIVISION_VALUES).mean() >= 0.9:
            return str(col)
    return None


def _pick_noise_tables(
    tables: list[tuple[str, pd.DataFrame]], path: Path
) -> list[tuple[str, pd.DataFrame]]:
    """1 冊の中から常時監視と要請限度の表を中身で選び、(調査名, 表) で返す。

    **2026-08-05 まで、このレイヤーは常時監視だけを読んでいた。**
    要請限度は苦情の出た道路を測る調査で、測定点の選ばれ方が区に依存する
    （`docs/issues.md` A1 の原因そのもの）ため、意図的に外していた。

    **入れることにした**（ユーザー判断）。理由は、
    **要請限度が測っているのは「実際にうるさいと申し立てが出た道路」**で、
    この作品が探しているもの——過負荷で退避先が要る場所——に直接効く情報だから。
    偏りは残る。だから**捨てずに、どちらの調査の点かを画面まで運ぶ**
    （`survey` 列）。混ぜたことを見えなくするのが一番まずい。

    **どちらも積極的な手掛かりで判定する。** 「常時監視でない表 = 要請限度」に
    すると、様式が変わって地域類型の列が落ちた常時監視の表を要請限度として
    読む。手掛かりは法概念の書き分けで、大文字/小文字は表記のゆれではない:

      - 常時監視 … 環境基準の地域類型が**大文字 A/AA/B/C**、
                    遮音壁等・低騒音舗装の有無（○/×）の列がある
      - 要請限度 … 騒音規制法の区域の区分が**小文字 a/b/c**、○×の列は無い
    """
    found: list[tuple[str, pd.DataFrame]] = []
    detail = []
    for name, df in tables:
        area_type, marks = _monitoring_markers(df)
        zone = _request_limit_marker(df)
        detail.append(
            f"  {name}: {len(df):,}行 / "
            f"地域類型（大文字 A/B/C）{area_type or '無し'} / "
            f"○×の列 {', '.join(marks) or '無し'} / "
            f"区域の区分（小文字 a/b/c）{zone or '無し'}"
        )
        if area_type is not None and marks:
            print(
                f"[noise] {SURVEY_MONITORING}の表を中身から特定: {name}"
                f"（地域類型 {area_type} が大文字 A/B/C・"
                f"○×の列 {', '.join(marks)}・{len(df):,}行）"
            )
            found.append((SURVEY_MONITORING, df))
        elif zone is not None:
            print(
                f"[noise] {SURVEY_REQUEST_LIMIT}の表を中身から特定: {name}"
                f"（区域の区分 {zone} が小文字 a/b/c・{len(df):,}行）"
            )
            found.append((SURVEY_REQUEST_LIMIT, df))

    if not found:
        raise ValueError(
            "\n".join(
                [
                    f"{path.name} に測定地点の表が無い。",
                    "常時監視の表は環境基準の地域類型を**大文字 A/AA/B/C** で持ち、"
                    "遮音壁等の有無（○/×）の列がある。要請限度は区域の区分を"
                    "**小文字 a/b/c** で持ち、○×の列を持たない。",
                    "",
                    "読めた表:",
                    *detail,
                    "",
                    "年度フォルダの「調査結果」の Excel（両方の表を含む）か、"
                    "「常時監視測定地点」「要請限度測定地点」の CSV を渡すこと。",
                ]
            )
        )
    kinds = [k for k, _ in found]
    if len(kinds) != len(set(kinds)):
        raise ValueError(
            "\n".join(
                [
                    f"{path.name} で同じ調査の表が複数に見える: {'・'.join(kinds)}。"
                    "どれを読むべきか決められない。",
                    "",
                    "読めた表:",
                    *detail,
                ]
            )
        )
    return found


def _surveyed_year(df: pd.DataFrame) -> str:
    """測定年度を**中身から**取る。取れなければ空文字。

    ファイル名からは当てない。ブラウザが化けさせた
    `cyousakekka$300500a20210401153420268.files$2019monitoring.csv` の
    `2021` はダウンロード時刻で、`2019` が年度である——**同じ名前に
    紛らわしい数字が 2 つ入っている。**

    **列名だけで決めない。** 見出しが 2 段の表を平坦化した結果は年度で揺れ、
    令和3年度の要請限度だけ「測定開始開始」（「測定開始」＋「開始」）になる。
    候補に無いので `pick_column` が外れ、**年度が空のまま静かに通っていた**
    ——画面は「6 年度の平均」と書きながら 5 年度しか並べない状態になり、
    241 地点でそうなっていた。列名で見つからなければ**中身**から探す。
    """
    col = pick_column(df, COLUMN_MAP["tokyo_road_noise"]["surveyed"])
    dates = pd.to_datetime(df[col], errors="coerce") if col else None

    if dates is None or not dates.notna().any():
        # 中身から探す。**測定期間の日付が入っている列**——他の日付列
        # （集計日など）は無いので、範囲で絞れば取り違えようがない。
        # 同点なら名前に手掛かりがある方を採る。
        best: tuple[float, str] | None = None
        for c in df.columns:
            parsed = pd.to_datetime(df[c], errors="coerce")
            hit = parsed.between("2000-01-01", "2035-12-31").mean()
            if hit < 0.8:
                continue
            score = hit + (0.5 if re.search(r"測定|開始|期間|年月日", str(c)) else 0)
            if best is None or score > best[0]:
                best = (score, str(c))
        if best is None:
            return ""
        col = best[1]
        dates = pd.to_datetime(df[col], errors="coerce")
        print(f"[noise] 測定年度の列を中身から特定: {col}")
    # 年度なので 4〜12 月はその年、1〜3 月は前年に寄せる。
    fiscal = dates.dt.year - (dates.dt.month < 4)
    year = int(fiscal.mode().iloc[0])
    print(f"[noise] 測定年度を {col} から特定: {year}年度（{int(dates.notna().sum()):,}行）")
    return str(year)


def _read_nies_gis_noise(path: Path) -> pd.DataFrame | None:
    """環境GIS＋（全国の自動車騒音常時監視結果）の CSV なら読む。違えば None。

    **判定はファイル名でも見出しの有無でもなく、中身の組み合わせで行う。**
    このファイルは全国 1 本・2002〜2024 年度で 69,833 行あり、
    都の年度別ファイルとは別物である。手掛かりは 3 つそろって初めて成立する:
    都道府県コード・地方公共団体コード・昼間の騒音の列。

    **使うのは `config.NOISE_GIS_YEARS` の年度だけ**（現在は 2024）。
    残りの年度は都の資料から直接取っており、そちらは要請限度も含む。
    全部読むと 2002 年からの測定が需要も負荷も持たないまま層に入る。

    **座標（`x`/`y`）は捨てる。** 小数第3位に丸めてあり、都の公表座標を
    基準にすると中央値 65m ずれる（当方のジオコーディングは中央値 0m）。
    250m 区画に載せる用途では粗いので、他の年度と同じく住所から作り直す。
    列を残すと `_fill_noise_coords_from_address` が拾い得るので、明示的に落とす。
    """
    try:
        head = read_csv_japanese(path, nrows=5, dtype=str)
    except Exception:
        return None
    cols = set(map(str, head.columns))
    if not ({"都道府県コード", "地方公共団体コード"} <= cols):
        return None
    if not any(c in cols for c in COLUMN_MAP["tokyo_road_noise"]["laeq_db"]):
        return None

    df = read_csv_japanese(path, dtype=str)
    years = tuple(str(y) for y in NOISE_GIS_YEARS)
    have = set(df["測定年度"].dropna().astype(str))
    missing = [y for y in years if y not in have]
    if missing:
        raise ValueError(
            f"{path.name}: 求めている年度 {'・'.join(missing)} がこのファイルに無い"
            f"（収録は {min(have)}〜{max(have)}）。"
            "config.NOISE_GIS_YEARS と配信年度を確認すること。"
        )

    # 都内に絞る。**全国 69,833 行をそのまま座標化しない**——位置参照情報は
    # 都内しか読んでおらず、他県の住所は全件落ちる（そして「照合できない住所が
    # 5% を超えたら止める」に引っかかって、正しい入力なのに止まる）。
    keep = df["測定年度"].astype(str).isin(years) & (df["都道府県名"] == "東京都")
    df = df[keep].drop(columns=[c for c in ("x", "y") if c in df.columns])
    require_nonempty(
        len(df),
        tag="noise",
        what=f"環境GIS＋の東京都・{'・'.join(years)}年度の行",
        why="年度と都道府県名の列を確認すること。",
    )
    print(
        f"[noise] 環境GIS＋（全国）から東京都の {'・'.join(years)}年度 "
        f"{len(df):,}行を採る（座標は使わず住所から作り直す）"
    )
    return df


def normalize_noise(path: Path) -> gpd.GeoDataFrame:
    """東京都環境局の自動車交通騒音の測定地点を点データにする。

    **1 冊から常時監視と要請限度の両方を読む**（`_pick_noise_tables`）。
    どちらの調査で測った点かは `survey` 列で最後まで持ち回る——測定点の
    選ばれ方が違う 2 つの調査を混ぜている以上、**混ぜたことが画面から
    見えなくなってはいけない**（`docs/issues.md` A1）。

    騒音レベルの列を取り違えると全件 NaN になり、
    測定点が存在するのに騒音レイヤーの値が消える
    （「点を面に変換する」という本作の主張ごと消える）。

    **5 年分を並べて渡す。** 常時監視は幹線道路をローテーションして測るので、
    1 年度分は 23 区で約 150 点しかないが、5 年分の和集合は 700 点になる
    （複数年で測られた地点は 26 点だけ）。束ねるのは `aggregate_noise_years`。
    """
    gis = _read_nies_gis_noise(path)
    if gis is not None:
        # 環境GIS＋は常時監視だけを収録している（要請限度は入っていない）。
        # 中身でもそれを確かめる——`環境基準類型コード` が大文字 A/AA/B/C。
        area_type, _ = _monitoring_markers(gis)
        if area_type is None:
            raise ValueError(
                f"{path.name}: 環境GIS＋のはずが環境基準の地域類型"
                "（大文字 A/AA/B/C）の列が無い。列構成が変わった可能性がある。"
            )
        return _normalize_noise_table(
            gis, path, SURVEY_MONITORING, SOURCES["nies_gis_road_noise"].label
        )

    frames = [
        _normalize_noise_table(df, path, survey, SOURCES["tokyo_road_noise"].label)
        for survey, df in _pick_noise_tables(_read_noise_tables(path), path)
    ]
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=frames[0].crs
    ).reset_index(drop=True)


def _normalize_noise_table(
    df: pd.DataFrame, path: Path, survey: str, source_label: str
) -> gpd.GeoDataFrame:
    """1 つの表（常時監視 or 要請限度）を点データにする。"""
    # **騒音レベルの列は座標化より先に決める。** 緯度（35.6〜35.8）は
    # `NOISE_DB_RANGE` に入ってしまうので、住所から作った緯度の列が表に
    # 加わったあとだと dB の候補が 1 つ増える。いまは `name_hint=r"昼"` が
    # 効いているので結果は変わらないが、**候補を増やす順番で呼ぶ理由が無い。**
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
    df, geocoded = _fill_noise_coords_from_address(df, path)
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
    name_col = optional_column(
        df,
        "tokyo_road_noise",
        "name",
        tag="noise",
        label="測定地点名",
        fallback="根拠カードに地点名が出ない",
    )

    year = _surveyed_year(df)

    gdf = clip_to_study_area(_to_points(df, lon_col, lat_col))
    require_nonempty(
        len(gdf),
        tag="noise",
        what="研究領域内の測定点",
        why=(
            f"{len(df):,}件を読んだが対象区に入るものが無い。"
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
        f"[noise] {survey}: 研究領域内 {len(gdf):,}件 / "
        f"{laeq_col} = {gdf['laeq_db'].min():.0f}〜{gdf['laeq_db'].max():.0f} dB"
        + ("（住所から座標化）" if geocoded else "")
    )
    gdf["name"] = gdf[name_col] if name_col else ""
    gdf["year"] = year
    gdf["survey"] = survey
    # **出典は表ごとに違う。** 令和元〜5年度は都の資料、令和6年度は環境GIS＋。
    # ここを固定にしていると、2024 年度の点が都の資料を出典として名乗る。
    gdf["source"] = source_label
    gdf["synthetic"] = False
    return gdf[
        [
            "name",
            "laeq_db",
            "year",
            "survey",
            "lon",
            "lat",
            "source",
            "synthetic",
            "geometry",
        ]
    ].reset_index(drop=True)


def _fill_noise_coords_from_address(
    df: pd.DataFrame, path: Path
) -> tuple[pd.DataFrame, bool]:
    """常時監視の測定地点を住所から座標化する。(座標のある行, 埋めたか) を返す。

    **常時監視測定地点は緯度経度を持たない。** 公表されているのは
    「千代田区平河町2丁目6」という住所だけで、平成25年度まで配信されていた
    「自動車交通騒音調査結果」の CSV にあった緯度経度の列が無い。

    **推定に切り替えるのではなく、同じ座標を作り直している。** 平成25年度の
    ファイルは住所と緯度経度を両方持つので、そこで裏を取れる——23 区内
    383 行のうち **381 行が街区レベルで一致し、公表座標との距離は中央値 0m**
    （90%点 46m・最大 249m）。**都の公表座標そのものが位置参照情報の
    街区代表点**だった。つまり座標の作り方は従来と変わらない。
    """
    lat_col = pick_column(df, COLUMN_MAP["tokyo_road_noise"]["lat"])
    lon_col = pick_column(df, COLUMN_MAP["tokyo_road_noise"]["lon"])
    if lat_col and lon_col:
        have = pd.to_numeric(df[lat_col], errors="coerce").between(
            *TOKYO_LAT_RANGE
        ) & pd.to_numeric(df[lon_col], errors="coerce").between(*TOKYO_LON_RANGE)
        if have.mean() >= COORD_MIN_RATIO:
            return df, False

    addr_col = require_column(
        df,
        path,
        "tokyo_road_noise",
        "address",
        tag="noise",
        label="測定地点の住所",
        why=(
            "常時監視測定地点には緯度経度が無く、住所からしか座標を作れない。"
            "特定できないまま進めると測定点が 1 件も地図に載らない。"
        ),
        predicate=lambda s: s.astype(str).str.match(
            r"^(?:東京都)?(?:" + "|".join(TOKYO_23_WARDS) + r")"
        ),
        # 23 区外（多摩・島しょ）の行が同じファイルに入っている。
        # 常時監視は都内 267〜285 行のうち 23 区が 144〜157 行なので、
        # 一致率は 5 割前後にしかならない。
        min_ratio=0.4,
        name_hint=r"住所|地点",
    )
    # **23 区分だけ座標化してはいけない。** このファイルは都全域が 1 本で、
    # 23 区の行は半分ほどしかない。区名で先に絞ると多摩の測定点が消え、
    # **区界のすぐ外を測った点まで落ちて縁のメッシュが不自然に静かに出る**
    # （`config.CLIP_BUFFER_M` / `issues.md` A8）。都内全市区町村で座標化して、
    # 範囲の絞り込みは他のレイヤーと同じく `clip_to_study_area` に任せる。
    geocoder = geocode.load((geocode.ALL_MUNICIPALITIES,))
    lat, lon = geocode.geocode_column(df[addr_col], geocoder, tag="noise")
    df = df.assign(緯度=lat, 経度=lon)

    # **取りこぼしは 23 区の中だけで測る。** 全体の一致率で見ると、
    # 島しょ（大島町・八丈町）が落ちた分と 23 区の住所の書き方が変わった分が
    # 混ざり、**この作品にとって痛い方だけを見られない**。
    in_23ku = (
        df[addr_col]
        .astype(str)
        .str.match(r"^(?:東京都)?(?:" + "|".join(TOKYO_23_WARDS) + r")")
    )
    missing = in_23ku & df["緯度"].isna()
    lost, total = int(missing.sum()), int(in_23ku.sum())
    if lost:
        print(
            f"[noise] 23 区内なのに照合できない住所 {lost}/{total}件: "
            f"{list(df.loc[missing, addr_col].head(5))}",
            file=sys.stderr,
        )
    # 取りこぼしが 5% を超えたら止める。**過小計上は出力を見ても気付けない**
    # （測定点が薄い区は「静か」ではなく「測っていない」と同じ見た目になる）。
    if total and lost / total > 0.05:
        raise ValueError(
            f"{path.name}: 23 区内 {total}件のうち {lost}件を"
            "座標化できない。住所の書き方が変わった可能性がある"
            "（etl/geocode.py の候補の並べ方を確認すること）。"
        )
    return df[df["緯度"].notna()].reset_index(drop=True), True


def aggregate_noise_years(frames: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    """年度ごとの測定結果を 1 つの点データに束ねる。

    **同じ地点を複数年で測っていることがある**（5 年分 700 点のうち 26 点）。
    そのまま重ねても IDW の重みが等しいので平均と同じ結果になるが、
    画面に「何年度の値か」を出せなくなるため、ここで明示的に平均する。
    年次のばらつきは小さい——同一地点の年度間標準偏差は**中央値 0.55dB**で、
    帯域の目安（`issues.md` A4）どころか測定の丸め（1dB 単位）と同じ桁である。

    **住所ではなく座標で束ねる。** 同じ街区に別々の住所で 2 点あるとき、
    位置参照情報は同じ代表点を返す。住所で束ねると同一座標に 2 点が残り、
    IDW では「その街区だけ重みが 2 倍」になる。地図で見ても点が重なって
    見えないので気付けない。
    """
    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    # 位置参照情報が配る座標の桁で束ねる。同じ街区なら同じ代表点が返るので、
    # 距離で寄せる（`dedupe_points`）必要は無い——**距離で寄せてはいけない**。
    # 100m 以内の別の街区にも測定点があり、寄せると別々の道路の実測値が
    # 1 点に潰れる。
    key = (
        out["lon"].round(_ISJ_DECIMALS).astype(str)
        + ","
        + out["lat"].round(_ISJ_DECIMALS).astype(str)
    )
    grouped = out.groupby(key, sort=False)

    merged = grouped.agg(
        name=("name", "first"),
        laeq_db=("laeq_db", "mean"),
        lon=("lon", "first"),
        lat=("lat", "first"),
        # **出典も併記する。** 同じ地点を令和5年度（都の資料）と令和6年度
        # （環境GIS＋）が測っていることがある。`first` にすると、その地点は
        # **片方の出典だけを名乗る**——画面は測定点の吹き出しに出典を出すので、
        # 2024 年度の値を都の資料の出典で出すことになる。
        source=(
            "source",
            lambda s: SOURCE_JOIN.join(sorted({str(v) for v in s if v})),
        ),
        synthetic=("synthetic", "first"),
        geometry=("geometry", "first"),
        # **件数と一覧は同じ集合から作る。** 別々に作っていたため、
        # 画面が「6 年度の平均」と書きながら 5 年度しか並べない点が 241 あった
        # ——`nunique()` は年度を取れなかった行の空文字を 1 種類として数え、
        # 一覧の側はそれを落としていた。**どちらが正しいかではなく、
        # 2 つの数字が別の場所から来ていたことが誤り。**
        years=("year", lambda s: "・".join(sorted({str(v) for v in s if v}))),
        # **同じ街区を両方の調査が測っていることがある。** そこも 1 点に畳む
        # ——上と同じ理由で、同一座標に 2 点残すとその街区だけ IDW の重みが
        # 2 倍になる。**どちらの調査で測ったかは捨てずに並べて持つ**
        # （「常時監視・要請限度」）。混ぜたことが画面から見えなくなるのが
        # いちばんまずい（`docs/issues.md` A1）。
        survey=(
            "survey",
            lambda s: "・".join(sorted({str(v) for v in s if v})),
        ),
    ).reset_index(drop=True)
    merged["laeq_db"] = merged["laeq_db"].round(1)
    merged["n_years"] = (
        merged["years"].str.count("・").add(1).where(merged["years"].ne(""), 0)
    )

    # 年度を取れなかった行があると、その地点の年度が 1 つ少なく出る。
    # **黙って通さない**——測定年度は画面に出る値で、`_surveyed_year` が
    # 空を返すのは「測定年月日の列を見つけられなかった」ときだけである。
    blank_year = int((out["year"].astype(str) == "").sum())
    if blank_year:
        print(
            f"[noise] 測定年度を取れなかった測定 {blank_year:,}件"
            "（`測定開始年月日` などの列を確認すること）",
            file=sys.stderr,
        )

    repeated = int((merged["n_years"] > 1).sum())
    both = int((merged["survey"].str.contains("・")).sum())
    by_survey = merged["survey"].value_counts().to_dict()
    print(
        f"\n[noise] {len(out):,}件の測定を {len(merged):,}地点に束ねた"
        f"（複数年度で測られた地点 {repeated:,}・"
        f"両方の調査が測った地点 {both:,}）"
    )
    print(
        "[noise] 調査ごとの地点数: "
        + " / ".join(f"{k} {v:,}" for k, v in sorted(by_survey.items()))
    )
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=out.crs)


# 公表された座標がこの割合を下回ったら、住所からの座標化へ切り替える。
# 「列は在るが全行空」（千代田区 230 行）と「一部だけ欠測」（荒川区 52%）の
# 両方を同じ経路で拾う。公表値がある行はそのまま残し、欠けている行だけ埋める。
COORD_MIN_RATIO = 0.95


def _fill_coords_from_address(df: pd.DataFrame, path: Path) -> bool:
    """緯度経度が欠けている行を、住所から埋める。埋めたら True。

    **公表された座標より優先しない。** 位置参照情報は街区の代表点なので、
    施設が公表している座標のほうが常に確からしい。欠けている行だけを埋める。

    座標が無いだけで区ごと落とすと、供給側の網羅性が区によって変わり、
    「到達不可」が実態ではなくデータの有無で決まる（docs/issues.md A9）。
    """
    lat_col = pick_column(df, COLUMN_MAP["tokyo_public_facility"]["lat"])
    lon_col = pick_column(df, COLUMN_MAP["tokyo_public_facility"]["lon"])
    addr_col = pick_column(df, COLUMN_MAP["tokyo_public_facility"]["address"])

    have = pd.Series(False, index=df.index)
    if lat_col and lon_col:
        have = pd.to_numeric(df[lat_col], errors="coerce").between(
            *TOKYO_LAT_RANGE
        ) & pd.to_numeric(df[lon_col], errors="coerce").between(*TOKYO_LON_RANGE)
        if have.mean() >= COORD_MIN_RATIO:
            return False

    if addr_col is None:
        # 座標も住所も無い。require_column が実際の列を添えて止める。
        return False

    missing = ~have
    print(
        f"[facilities] {path.name}: 公表座標が {int(have.sum()):,}/{len(df):,} 行。"
        f"残り {int(missing.sum()):,} 行を住所（{addr_col}）から補う"
    )
    geocoder = geocode.load(TOKYO_23_WARDS)
    lat, lon = geocode.geocode_column(
        df.loc[missing, addr_col], geocoder, tag="facilities"
    )
    if lat_col is None or lon_col is None:
        lat_col, lon_col = "緯度", "経度"
        df[lat_col] = pd.NA
        df[lon_col] = pd.NA
    df.loc[missing, lat_col] = lat
    df.loc[missing, lon_col] = lon
    return True


def normalize_hosts(path: Path) -> gpd.GeoDataFrame:
    """公共施設一覧をホスト施設候補にする。

    ホスト種別は施設名から判定する（schema.HOST_TYPE_PATTERNS）。
    つまり **名称の列を取り違えると候補が 0 件になり、
    「既存施設では到達不可」の件数が実態より多く出る** ——
    しかもレイヤーは実データとして数えられるので警告も出ない。
    区ごとに様式が違うデータなので、列名の決め打ちが最も外れやすい。
    """
    df = read_csv_japanese(path)
    geocoded = _fill_coords_from_address(df, path)
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
    # 種別列。あれば名称推定より確かなので併用する（無い区のほうが多い）。
    category_col = optional_column(
        df,
        "tokyo_public_facility",
        "category",
        tag="facilities",
        label="施設種別",
        fallback="施設名からの判定だけになる",
    )
    categories = (
        df[category_col].astype(str) if category_col else pd.Series("", index=df.index)
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
        # 目印にする。一覧には対象外の施設（学校・保育園・駐輪場）も含まれるため、
        # 全件一致は期待できない。**一致率の下限では列を選べない。**
        # 23 区分を通した実測で 世田谷区 19.5% / 新宿区 24.7% / 葛飾区 21.8% に対し
        # 豊島区は 4.1%（538 行中 22 件）まで開く。区によって一覧に載せる施設の
        # 範囲が違うだけで、名称列そのものは正しい。
        # 下限は「その列が名前の列でないこと」を弾く役割に留め、
        # 実質の保証は列名の手掛かり（require_hint）に置く。
        # 「地方公共団体名」も手掛かりには一致するが、値が区名の繰り返しで
        # ホスト判定が 0% になるためここで落ちる。
        # **種別列も一緒に見る。** 名称だけで判定すると、港区のように
        # 「芝地区総合支所」「芝の家」といった名称で、種別列（第2分類）に
        # 「総合支所・分室」「区民協働施設」と書いてある区を落とす。
        # 実際、名称だけの判定では 12 行中 0 件となり列ごと弾かれていた。
        predicate=lambda s: pd.Series(
            [host_type(v, c) is not None for v, c in zip(s, categories)],
            index=s.index,
        ),
        min_ratio=0.02,
        # **施設名は施設ごとに違う。** 一致率の下限（2%）は分類列を弾けない——
        # 江東区で「列1」を諦めた理由がこれで、「小区分」5.3%・「担当課」2.4% も
        # 下限を超えるため、一致率だけでは 3 列のどれとも決められなかった。
        # 列全体の一意率を見ると「列1」1.00 に対し 0.06 / 0.04 で桁が違う。
        # 実在する名称列の一意率は 23 区分の実測で最小 0.986（板橋区）なので、
        # 0.5 は「分類列を弾く」役にだけ効き、名称列は落とさない。
        whole=lambda s: (
            s.astype(str).replace("nan", "").pipe(lambda t: t[t != ""]).nunique()
            / max((s.astype(str).replace("nan", "") != "").sum(), 1)
            >= 0.5
        ),
        name_hint=r"施設|名称|名前|名$",
        require_hint=True,
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
            f"{len(df):,}件を読んだが対象区に入るものが無い。"
            f"{lat_col}/{lon_col} が緯度経度か確認すること。"
        ),
    )
    gdf["name"] = gdf[name_col]
    gdf_categories = (
        gdf[category_col].astype(str) if category_col else pd.Series("", index=gdf.index)
    )
    gdf["host_kind"] = [host_type(n, c) for n, c in zip(gdf["name"], gdf_categories)]
    gdf["ward"] = gdf[ward_col].map(normalize_ward) if ward_col else ""
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

# 昼間人口だけはメッシュコードで直接結合する表（点でも面でもない）。
POPULATION_FILE = "population.csv"


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

    pop_path = DATA_PROCESSED / POPULATION_FILE
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
    "s12": ("stations", "normalize_stations"),
    "estat-pop": ("population", "normalize_population"),
    "p13": ("parks", "normalize_parks"),
    "noise": ("noise", "normalize_noise"),
    "facilities": ("hosts", "normalize_hosts"),
}


def run_normalizer(
    kind: str,
    paths: list[Path],
    assume_missing: bool = False,
    merge: str | None = None,
    students_path: Path | None = None,
) -> Path:
    """指定した正規化を実行し、data/processed へ書き出す。

    ファイルは複数渡せる（区ごとに分かれた公共施設一覧など）。
    まとめて正規化し、同一施設を寄せてから 1 ファイルに書く。

    assume_missing は「その列が本当に収録されていない年度」のための逃げ道。
    受け付ける正規化（生徒数・定員）にだけ渡す。既定では渡さない ——
    列を取り違えたときに既定値で埋めて通してしまうのを防ぐのが目的なので、
    逃げ道は明示的に指定したときだけ開く。

    merge は既に別の出典が入っているレイヤーへ書くときの指定
    （"append" 統合 / "replace" 入れ替え）。既定は None で、
    出典が消える場合は書かずに止まる。

    students_path は在籍者数の別出典（p29 のみ）。paths と違って
    **地物ではなく属性を当てるための表**なので、並べて渡す paths とは
    別の引数にしてある（同じ列に混ぜると「どちらが位置の出典か」が消える）。
    """
    if kind not in NORMALIZERS:
        raise SystemExit(
            f"未知の種別 {kind!r}。使えるのは: {', '.join(sorted(NORMALIZERS))}"
        )
    layer_key, func_name = NORMALIZERS[kind]
    func = globals()[func_name]
    params = inspect.signature(func).parameters
    kwargs = {}
    if assume_missing:
        if "assume_missing" not in params:
            raise SystemExit(
                f"--assume-missing は種別 {kind!r} には無い。"
                "既定値で代用できる列を持つのは p29（生徒数）と wamnet（定員）だけ。"
            )
        kwargs["assume_missing"] = True
    if students_path is not None:
        if "students_path" not in params:
            raise SystemExit(
                f"--students は種別 {kind!r} には無い。在籍者数を外から当てるのは "
                "p29（特別支援学校）だけ。"
            )
        if not students_path.exists():
            inspect_columns(students_path)
            raise SystemExit("--students のファイルが見つからない。")
        kwargs["students_path"] = students_path

    # 存在しないパスをそのまま渡すと pandas / GDAL の traceback になり、
    # 「名前が違う」のか「壊れている」のか分からない。--inspect と同じ扱いで
    # 似た名前を並べて切り分ける。
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            inspect_columns(p)
        raise SystemExit("入力ファイルが見つからない。上の一覧から正しいパスを選ぶこと。")

    # 分割ファイルを並べて渡すときは、1 ファイルに対象範囲の行が無いことを
    # 許す（WAM NET はサービス種別ごとに 29 分割で、都内に十数件しかない
    # 種別は 2 区に 1 件も無い）。総数が 0 なら下で止める。
    global ALLOW_EMPTY_PER_FILE
    ALLOW_EMPTY_PER_FILE = len(paths) > 1

    frames = []
    skipped: list[str] = []
    for p in paths:
        if len(paths) > 1:
            print(f"\n=== {p.name} ===")
        try:
            frames.append(func(p, **kwargs))
        except EmptyInThisFile:
            skipped.append(p.name)
    ALLOW_EMPTY_PER_FILE = False

    if skipped:
        print(f"\n[normalize] 対象範囲に行が無く飛ばした {len(skipped)} ファイル: "
              f"{', '.join(skipped)}")
    if not frames:
        raise SystemExit(
            f"[{kind}] 渡した {len(paths)} ファイルのどれにも対象範囲の行が無い。"
            "入力ファイルと対象範囲（data/processed/area.geojson）を確認すること。"
        )
    # 束ね方はレイヤーによって違う。既定は「同名かつ 100m 以内なら同一施設」
    # （`dedupe_points`）だが、騒音は**施設ではなく測定**なので、同じ地点の
    # 別年度を平均する（`aggregate_noise_years`）。既定のまま通すと、
    # 100m 以内の別の街区の実測値まで 1 点に潰れる。
    if layer_key == "noise":
        gdf = aggregate_noise_years(frames)
    else:
        gdf = frames[0] if len(frames) == 1 else _concat_layers(frames)

    # 昼間人口はメッシュコードの表で、地物ではない（GeoJSON にならない）。
    if layer_key == "population":
        out = DATA_PROCESSED / POPULATION_FILE
        if len(frames) > 1:
            gdf = pd.concat(frames, ignore_index=True).drop_duplicates("mesh_code")
        gdf.to_csv(out, index=False)
        print(f"\n[normalize] {out.relative_to(out.parents[2])} に {len(gdf):,}件を書き出した")
        print("次: python -m etl.build --live")
        return out

    out = DATA_PROCESSED / PROCESSED_FILES[layer_key]
    gdf = _merge_with_existing(gdf, out, kind=kind, layer_key=layer_key, merge=merge)

    gdf.to_file(out, driver="GeoJSON")
    print(f"\n[normalize] {out.relative_to(out.parents[2])} に {len(gdf):,}件を書き出した")
    print("次: python -m etl.build --live")
    return out


def _concat_layers(frames: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=frames[0].crs
    )
    return dedupe_points(out, tag="merge")


def dedupe_points(gdf: gpd.GeoDataFrame, tag: str) -> gpd.GeoDataFrame:
    """同じ施設が 2 度入るのを防ぐ。

    区の公共施設一覧には P14 由来の児童館も載っている。両方入れると
    提言リストで同じ施設が 2 行に割れ、「何メッシュ分を受け持つか」が
    分散して費用対効果の説明が壊れる（build_proposals は施設名で束ねる）。

    名称が同じで位置が DEDUPE_DISTANCE_M 以内なら同一施設とみなす。
    座標は出典ごとに数十 m ずれる（建物重心 / 正面入口 / 住所ジオコーディング）ので
    完全一致では落ちない。

    座標を丸めて格子で寄せる方法は使わない。格子の境目に落ちた 2 点は
    数十 m しか離れていなくても別扱いになる（実際に 44m ずれの例で外した）。
    名称でまとめてから実距離で見る。同名の組は小さいので総当たりで足りる。

    **サービス種別（`kind`）は名称と併せて鍵にする。** 多機能型事業所は
    同じ住所・同じ名称で就労継続支援Ｂ型と生活介護を別々に届け出ており、
    WAM NET のオープンデータでも種別ごとに別ファイル・別行で入る。
    名称だけで寄せると、そのうち 1 行だけを残して**残りの定員を黙って捨てる**
    （需要スコアが下がる方向の誤りなので、地図を見ても気付けない）。
    """
    if "name" not in gdf.columns or len(gdf) == 0:
        return gdf

    # 全角空白・記号のゆれに加え、**同じ施設を別の書き方で載せている分だけ**を
    # 吸収する。かつては「区立」の有無も実質的な差として残していたが、
    # 23 区分を並べたところ **100m 以内に同一施設が 63 組**残っていた:
    #   「北新宿図書館」↔「新宿区立北新宿図書館」（新宿・中央・荒川・豊島・目黒）
    #   「祖師谷児童館」↔「祖師谷児童館（複合施設）」（世田谷）
    # 出典が違うだけで、どちらも同じ建物を指す。片方は P14、片方は区の一覧。
    #
    # **括弧を一律に外してはいけない。** 中野区の「もみじ山文化センター（本館）」と
    # 「（西館）」は別建物で、外すと 1 件に潰れる。落とすのは
    # 「（複合施設）」という世田谷区の注記だけに限る（施設を区別する語ではない）。
    # 「（旧）奥沢まちづくりセンター」も残る形にしてある（廃止施設は
    # `HOST_EXCLUDE_PATTERNS` の「旧」で別に落ちる）。
    norm = gdf["name"].astype(str).str.replace(r"[\s　・]", "", regex=True)
    norm = norm.str.replace(r"[（(]複合施設[)）]", "", regex=True)
    norm = norm.str.replace(
        r"^(?:" + "|".join(TOKYO_23_WARDS) + r")立?", "", regex=True
    )
    if "kind" in gdf.columns:
        norm = norm + "\x00" + gdf["kind"].astype(str)
    metric = gdf.to_crs(CRS_PROJECTED).geometry

    drop: set = set()
    for _, group in norm.groupby(norm):
        idx = list(group.index)
        if len(idx) < 2:
            continue
        kept: list = []
        for i in idx:
            if any(metric[i].distance(metric[j]) <= DEDUPE_DISTANCE_M for j in kept):
                drop.add(i)
            else:
                kept.append(i)

    if drop:
        print(
            f"[{tag}] 同名かつ {DEDUPE_DISTANCE_M:.0f}m 以内の施設 "
            f"{len(drop):,}件を統合した"
        )
    return gdf[~gdf.index.isin(drop)].reset_index(drop=True)


def _merge_with_existing(
    gdf: gpd.GeoDataFrame,
    out: Path,
    *,
    kind: str,
    layer_key: str,
    merge: str | None,
) -> gpd.GeoDataFrame:
    """既存の正規化済みデータを黙って捨てない。

    ひとつのレイヤーを複数の種別が書く。hosts は p14-hosts（児童館）と
    facilities（区の公共施設一覧）、welfare は p14 と wamnet。
    あとから流した方が既存を丸ごと置き換えるため、
    **児童館 154 件を消したことに気付かないまま「到達不可」が増える**
    といった壊れ方をする。出典が消えるなら止めて選ばせる。
    """
    if not out.exists() or merge == "replace":
        return gdf

    existing = gpd.read_file(out)
    if "source" not in existing.columns or "source" not in gdf.columns:
        return gdf

    lost = sorted(set(existing["source"]) - set(gdf["source"]))
    if not lost:
        # 同じ出典を入れ直しただけ。置き換えでよい。
        return gdf

    if merge is None:
        counts = existing["source"].value_counts()
        detail = "\n".join(f"        {s}: {counts.get(s, 0):,}件" for s in lost)
        raise SystemExit(
            f"\n{out.name} には、いま入れようとしている {kind} に含まれない出典がある:\n"
            f"{detail}\n\n"
            "このまま書くとその分が消える。どちらか選ぶこと:\n"
            f"    --append   既存と統合する（同一施設は名称と位置で 1 件に寄せる）\n"
            f"    --replace  既存を捨てて入れ替える\n"
        )

    # 入れ直した出典の古い行は先に捨てる。残したまま統合すると、同名・同位置の
    # 組では**先に並んでいる古い行が残り、正規化を直しても結果が変わらない**
    # （区名の表記を揃える修正が効かず、件数も出力サイズも同じままだった）。
    #
    # **旧 label（`Source.aliases`）も同じ出典として見る。** ここが現在の
    # label しか見ておらず、**同じ壊れ方が alias 経由で再発していた**
    # （2026-08-13 に発覚）。公共施設一覧の label を
    # 「公共施設一覧（図書館・文化施設・区民センター等）」から
    # 「各区の公共施設一覧（23 区・様式は区ごとに異なる）」へ改名した結果:
    #
    #   1. 古い行が stale に当たらず残る
    #   2. 新しい行は「同名かつ 100m 以内」で**全部**重複として消える
    #   3. **1,207 件を読み直しても出力が 1 バイトも変わらない**
    #
    # しかも「1,541件を書き出した」と成功と表示される。**除外パターンを
    # 足しても効かない**ので、供給側が過大なまま静かに固定される。
    # 上のコメントが警告しているのと同じ事故を、改名がもう一度開けていた。
    stale = existing["source"].map(current_source_label).isin(set(gdf["source"]))
    if stale.any():
        print(f"[merge] 入れ直した出典の既存 {int(stale.sum()):,}件は捨てる")
    existing = existing[~stale]

    merged = _concat_layers([existing, gdf])
    print(f"[merge] 既存 {len(existing):,}件 + 新規 {len(gdf):,}件 → {len(merged):,}件")
    for src, n in merged["source"].value_counts().items():
        print(f"        {src}: {n:,}件")
    return merged


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
        nargs="+",
        metavar="種別/ファイル",
        help="正規化して data/processed へ書き出す。"
        f"「種別 ファイル [ファイル...]」の順。種別: {', '.join(sorted(NORMALIZERS))}",
    )
    ap.add_argument(
        "--append",
        action="store_true",
        help="既に入っている別出典と統合する（同一施設は名称と位置で 1 件に寄せる）",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="既に入っている別出典を捨てて入れ替える",
    )
    ap.add_argument(
        "--students",
        type=Path,
        metavar="CSV",
        help="特別支援学校の在籍者数 CSV（p29 のみ）。"
        "東京都教育委員会「特別支援学校（学校別在籍者数）」",
    )
    args = ap.parse_args(argv)

    if args.inspect:
        inspect_columns(args.inspect)
        return 0

    if args.normalize:
        if len(args.normalize) < 2:
            ap.error("--normalize には種別とファイルが要る（例: --normalize p14 <ファイル>）")
        if args.append and args.replace:
            ap.error("--append と --replace は同時に指定できない")
        kind, *files = args.normalize
        run_normalizer(
            kind,
            [Path(f) for f in files],
            assume_missing=args.assume_missing,
            merge=("append" if args.append else "replace" if args.replace else None),
            students_path=args.students,
        )
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
