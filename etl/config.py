"""
calmgap-tokyo — プロジェクト全体の設定。

対象地域・座標系・メッシュ次数・スコア構成要素の定義と既定重み、
および各オープンデータの取得元レジストリを一箇所に集約する。
ETL の各モジュールはここだけを参照し、定数をハードコードしない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
WEB_DATA = ROOT / "web" / "public" / "data"

for _p in (DATA_RAW, DATA_PROCESSED, WEB_DATA):
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 座標系
# ---------------------------------------------------------------------------

# 配信・入出力は WGS84。距離・面積計算は平面直角座標系第IX系（東京都）で行う。
CRS_GEOGRAPHIC = "EPSG:4326"
CRS_PROJECTED = "EPSG:6677"  # JGD2011 / Japan Plane Rectangular CS IX

# ---------------------------------------------------------------------------
# 対象地域
# ---------------------------------------------------------------------------

# MVP は 2 区に限定する（ハンドオフ 5. Phase 1）。
# 渋谷区 = 巨大ターミナルと繁華街、世田谷区 = 大規模住宅地。
# 「需要 × 負荷」の掛け算モデルが、住宅地の需要を正しく落とすことを示すための対比。
TARGET_WARDS: list[str] = ["渋谷区", "世田谷区"]

# 行政区域コード（国土数値情報 N03_007）。
# SHP の DBF が Shift-JIS で読めない環境でも確実に絞り込めるよう、
# 名称とコードの両方で判定する。渋谷区=13113 / 世田谷区=13112。
TARGET_WARD_CODES: list[str] = ["13113", "13112"]

# 行政界（N03）が未取得のときに使う暫定の矩形。
# 実データが入ると etl/fetch.study_bbox() が区界から自動導出した値に置き換わる。
#
# ⚠️ この矩形は対象2区より広く、港区・目黒区・品川区の一部を含んでしまう。
#    実際、暫定矩形のままだと港区の施設が提言に出た。さらに上限 35.6900 は
#    渋谷区の北端 35.6920 を切り落としていた。行政界の投入で両方とも解消する。
STUDY_BBOX = (139.5500, 35.5900, 139.7350, 35.6950)  # (minx, miny, maxx, maxy)

# 全国データを研究領域へ絞り込む際、区界の外側にどれだけ余白を取るか。
#
# 区界ちょうどで切ると、境界のすぐ外にある事業所や駅が落ちる。
# 需要は 800m の距離減衰カーネルで周囲へ及ぶので、境界外の施設も
# 区内メッシュの需要を確かに生んでいる。ここを切ると縁のメッシュだけ
# 需要が不自然に低く出る（エッジ効果）。
# メッシュ自体は区界で厳密に切り、入力データだけ余白付きで拾う。
CLIP_BUFFER_M = 2000.0

# ---------------------------------------------------------------------------
# メッシュ
# ---------------------------------------------------------------------------

# 5 = 5次メッシュ(250m) / 4 = 4次メッシュ(500m)。JIS X 0410 準拠。
MESH_LEVEL = 5

# 徒歩圏の定義。点データをメッシュへ配分する距離減衰カーネルの帯域。
# 800m ≒ 徒歩10分。感覚過敏当事者は移動そのものが負荷になるため、
# 一般的な生活圏(1km)よりやや短く取る。
WALK_BANDWIDTH_M = 800.0

# レイヤーごとの帯域。「その施設の影響がどこまで及ぶか」は施設種別で異なる。
BANDWIDTH_M: dict[str, float] = {
    # 通所の徒歩圏。基準値。
    "welfare_capacity": WALK_BANDWIDTH_M,
    # 特別支援学校は広域から通学し、送迎車両の往来も周辺に広がるため長く取る。
    "sped_school": 1200.0,
    # 駅の過負荷（雑踏・アナウンス・改札）は駅前で急激に減衰する。
    "station_flow": 600.0,
    # 通院は徒歩圏が基本。
    "clinic": WALK_BANDWIDTH_M,
}

# ---------------------------------------------------------------------------
# スコア構成要素
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """スコアを構成する 1 レイヤー。

    key       : メッシュ属性名・フロント側のスライダー ID
    label     : UI 表示名（日本語）
    side      : "demand" | "load"
    weight    : 既定重み（UI スライダーの初期値）
    sign      : +1 = 値が大きいほどスコア増、-1 = 減点レイヤー
    source    : 出典表示（根拠カード・methodology 用）
    rationale : なぜこの指標なのかの一文

    zero_is_absence:
        True  = 値 0 は「存在しない」を意味する（事業所が無い、公園が無い）。
                正規化後も厳密に 0 とし、掛け算モデルで確実に効かせる。
        False = 値 0 も連続量の一点にすぎない（用途地域スコア、騒音レベル）。
                順位に応じて 0〜1 へ素直に配分する。

        この区別を付けないと、事業所が 1 件も無いメッシュ群が
        「ゼロの塊の平均順位」として 0.3 程度の需要を持ってしまい、
        住宅地を落とすという掛け算モデルの狙いが崩れる。
    """

    key: str
    label: str
    side: str
    weight: float
    sign: int
    source: str
    rationale: str
    zero_is_absence: bool = True


DEMAND_COMPONENTS: tuple[Component, ...] = (
    Component(
        key="welfare_capacity",
        label="障害福祉サービス事業所（定員重み）",
        side="demand",
        weight=1.0,
        sign=1,
        source="WAM NET 障害福祉サービス等事業所一覧 / 国土数値情報 P14",
        rationale="就労移行・就労継続A/B・生活介護・放課後等デイ等の定員は、"
        "その徒歩圏を日常的に往復せざるを得ない当事者の実数に最も近い代理変数。",
    ),
    Component(
        key="sped_school",
        label="特別支援学校（規模重み）",
        side="demand",
        weight=0.8,
        sign=1,
        source="国土数値情報 P29 学校",
        rationale="通学は選択の余地がない移動であり、児童生徒本人と送迎者の双方が"
        "同一時間帯に同一地点へ集中する。",
    ),
    Component(
        key="station_flow",
        label="駅乗降規模",
        side="demand",
        weight=0.9,
        sign=1,
        source="ODPT 公共交通オープンデータ / 東京都統計年鑑",
        rationale="「通わざるを得ない過負荷な場所」の中核。乗降客数は"
        "その地点を通過せざるを得ない人の総量を表す。",
    ),
    Component(
        key="clinic",
        label="精神科・心療内科",
        side="demand",
        weight=0.5,
        sign=1,
        source="国土数値情報 P04 医療機関",
        rationale="受診当日は体調が不安定なことが多く、"
        "退避先の必要性が最も高い移動のひとつ。",
    ),
)

LOAD_COMPONENTS: tuple[Component, ...] = (
    Component(
        key="zoning",
        label="用途地域（法的な騒がしさ上限）",
        side="load",
        weight=1.0,
        sign=1,
        source="不動産情報ライブラリ API / 国土数値情報 A29 用途地域",
        rationale="本作独自の転用。用途地域は「その土地が法的にどこまで"
        "騒がしくなり得るか」の上限を定めた規制であり、"
        "実測点が疎な感覚負荷を面として推定する予測器として使える。",
        zero_is_absence=False,  # 全メッシュが何らかの用途地域スコアを持つ連続量
    ),
    Component(
        key="noise",
        label="自動車・鉄道騒音",
        side="load",
        weight=0.9,
        sign=1,
        source="東京都環境局 自動車騒音要請限度測定結果 / 鉄道騒音振動調査",
        rationale="聴覚過敏の直接要因。点測定を距離重み付き内挿で面に変換する。",
        zero_is_absence=False,  # 内挿後は全メッシュが騒音レベルを持つ連続量
    ),
    Component(
        key="crowding",
        label="混雑（昼間人口）",
        side="load",
        weight=0.8,
        sign=1,
        source="e-Stat 地域メッシュ統計 昼間人口 / 国勢調査",
        rationale="人的密度そのものが視覚・聴覚・触覚の同時多重刺激を生む。",
    ),
    Component(
        key="green",
        label="緑・公園被覆（減点）",
        side="load",
        weight=0.6,
        sign=-1,
        source="国土数値情報 P13 都市公園 / 東京都 緑被率調査",
        rationale="既に安らげる空間が担保されている場所は、"
        "新規整備の優先度を下げてよい。負の負荷として減点する。",
    ),
)

ALL_COMPONENTS: tuple[Component, ...] = DEMAND_COMPONENTS + LOAD_COMPONENTS

# 需要スコアと負荷スコアの合成指数。priority = demand^ALPHA * load^BETA
# 1.0 / 1.0 は素直な掛け算。掛け算にする理由はハンドオフ 3. のとおり、
# 「人がいて、かつ過負荷」の両方が揃った場所だけを立てるため。
PRIORITY_ALPHA = 1.0
PRIORITY_BETA = 1.0

# 根拠カードを生成する上位メッシュ数
TOP_N_CARDS = 20

# 配信 JSON における正規化値の小数桁。
#
# ここで丸めた値がブラウザへ渡り、スライダー操作のたびに再計算される。
# したがって Python 側も「丸めた後の値」から demand/load/priority を計算しなければ、
# 初期表示（Python の計算結果）とスライダーを既定値に戻したとき（ブラウザの計算結果）で
# 色が食い違う。tools/parity_check.mjs がこの一致を検証している。
PUBLISH_DECIMALS = 4

# ---------------------------------------------------------------------------
# ホスト施設（供給側・提言の割当先）
# ---------------------------------------------------------------------------

# 上位メッシュに割り当てる公共施設の種別と、割当時の優先度。
# 数値が小さいほど「カームダウンスペースを置きやすい」と評価する。
# 図書館は静穏性が既に運営方針に組み込まれており、個室化の合意形成が最も容易。
HOST_PREFERENCE: dict[str, int] = {
    "図書館": 1,
    "区民センター": 2,
    "出張所": 2,
    "文化施設": 3,
    "児童館": 3,
    "公園管理施設": 4,
    "駅": 5,
}

# 割当を認める最大距離。これを超える場合は「候補施設なし＝新設が必要」と提言する。
HOST_MAX_DISTANCE_M = 700.0

# ---------------------------------------------------------------------------
# データソース レジストリ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """オープンデータ 1 件の取得定義。fetch.py が参照する。"""

    key: str
    label: str
    url: str
    kind: str  # "csv" | "shp" | "geojson" | "api" | "excel" | "manual"
    license: str
    note: str = ""
    api_key_env: str | None = None
    params: dict = field(default_factory=dict)


SOURCES: dict[str, Source] = {
    # --- 需要レイヤー ---
    "wamnet_jigyosho": Source(
        key="wamnet_jigyosho",
        label="障害福祉サービス等事業所一覧（所在地・定員）",
        url="https://www.wam.go.jp/content/wamnet/pcpub/top/sfkopendata/",
        kind="csv",
        license="WAM NET 二次利用可（出典表示）",
        note="都道府県別 CSV。東京都分を抽出し住所からジオコーディングする。",
    ),
    "ksj_p29_school": Source(
        key="ksj_p29_school",
        label="国土数値情報 P29 学校（特別支援学校を含む）",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P29.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="P29_004 学校分類コード 16 = 特別支援学校。P29_009 に児童生徒数。",
    ),
    "ksj_p14_welfare": Source(
        key="ksj_p14_welfare",
        label="国土数値情報 P14 福祉施設（定員つき）",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P14-v2_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="WAM NET のジオコーディング失敗分を補完する座標つきデータ。",
    ),
    "ksj_p04_medical": Source(
        key="ksj_p04_medical",
        label="国土数値情報 P04 医療機関",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P04-v3_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="診療科目欄から精神科・心療内科を抽出する。",
    ),
    "odpt_station": Source(
        key="odpt_station",
        label="ODPT 駅・乗降人員",
        url="https://api.odpt.org/api/v4/odpt:Station",
        kind="api",
        license="ODPT 公共交通オープンデータ 利用規約",
        api_key_env="ODPT_ACCESS_TOKEN",
        note="乗降人員は odpt:PassengerSurvey を併用。無い駅は都統計年鑑で補完。",
    ),
    "tokyo_techo": Source(
        key="tokyo_techo",
        label="精神障害者保健福祉手帳 交付状況（区市町村別）",
        url="https://www.fukushi.metro.tokyo.lg.jp/",
        kind="excel",
        license="東京都 オープンデータ（CC BY 4.0 相当）",
        note="区単位の全体係数としてのみ使用。集計統計のみを扱い個人特定粒度では表示しない。",
    ),
    # --- 負荷レイヤー ---
    "reinfolib_youto": Source(
        key="reinfolib_youto",
        label="用途地域（不動産情報ライブラリ API）",
        url="https://www.reinfolib.mlit.go.jp/ex-api/external/XKT013",
        kind="api",
        license="不動産情報ライブラリ 利用規約（出典表示）",
        api_key_env="REINFOLIB_API_KEY",
        note="ズームレベル・タイル座標指定。取得不可時は国土数値情報 A29 で代替。",
    ),
    "ksj_a29_youto": Source(
        key="ksj_a29_youto",
        label="国土数値情報 A29 用途地域",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A29-v2_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="reinfolib_youto の代替。A29_005 に用途地域コード。",
    ),
    "tokyo_road_noise": Source(
        key="tokyo_road_noise",
        label="自動車騒音 要請限度測定結果",
        url="https://catalog.data.metro.tokyo.lg.jp/dataset/t000010d0000000041",
        kind="csv",
        license="東京都オープンデータカタログ CC BY 4.0",
        note="測定地点の点データ。等価騒音レベル LAeq を距離重み付き内挿する。",
    ),
    "tokyo_rail_noise": Source(
        key="tokyo_rail_noise",
        label="鉄道騒音・振動調査結果",
        url="https://catalog.data.metro.tokyo.lg.jp/dataset/t000010d0000000042",
        kind="csv",
        license="東京都オープンデータカタログ CC BY 4.0",
        note="測定点が疎なため、鉄道路線（KSJ N02）からの距離減衰で補完する。",
    ),
    "ksj_p13_park": Source(
        key="ksj_p13_park",
        label="国土数値情報 P13 都市公園",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P13.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="ポリゴンの面積被覆率をメッシュ単位で算出する。",
    ),
    "estat_mesh_pop": Source(
        key="estat_mesh_pop",
        label="地域メッシュ統計 人口・昼間人口",
        url="https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData",
        kind="api",
        license="政府統計 e-Stat 利用規約（出典表示）",
        api_key_env="ESTAT_APP_ID",
        note="メッシュコードで直接結合できる唯一のレイヤー。空間補間が不要。",
    ),
    # --- 供給側 ---
    "tokyo_public_facility": Source(
        key="tokyo_public_facility",
        label="公共施設一覧（図書館・文化施設・区民センター等）",
        url="https://catalog.data.metro.tokyo.lg.jp/dataset?q=公共施設",
        kind="csv",
        license="東京都オープンデータカタログ CC BY 4.0",
        note="区市町村ごとに様式が異なるため normalize 側で名寄せする。",
    ),
    # --- Phase 2 ---
    "existing_calmdown": Source(
        key="existing_calmdown",
        label="既存カームダウン・クールダウンスペース",
        url="",
        kind="manual",
        license="各施設公表情報（要出典明記）",
        note="中央集約されたオープンデータは存在しない。手作業の名寄せが必要。"
        "Phase 2。モデル妥当性の検証に用いる。",
    ),
}
