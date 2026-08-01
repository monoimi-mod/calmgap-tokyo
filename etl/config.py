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

# 渋谷区 = 巨大ターミナルと繁華街、世田谷区 = 大規模住宅地。
# 「需要 × 負荷」の掛け算モデルが、住宅地の需要を正しく落とすことを示すための対比。
#
# **新宿区と文京区を足した理由は見栄えではない。**
#
# 2 区だけで算出したとき、上位 10 メッシュが全て対象地域の外形境界から
# 500m 以内にあった（全メッシュでは 37.9%）。第 1 位は境界から 32m。
# 需要カーネルの帯域は 800m〜1,200m で、入力は区界の外側 2km まで拾っている。
# つまり境界付近のメッシュは需要の相当部分を**区外の施設**から得ており、
# その区外自身は評価されていない。上位が「新宿区に近い渋谷区」に
# 並ぶのは、新宿区を見ていないことの裏返しでしかない可能性があった。
#   → 新宿区を入れれば、本当のピークが境界の向こうかどうかを直接確かめられる。
#   → 文京区は作者が日常的に歩いている区。出力の妥当性を現地で照合できる
#     唯一の区で、issues.md D3（サニティチェックが機能していない）に効く。
#     必ず**先にスコアを出してから歩く**こと。順序を逆にすると後付けになる。
#
# 2026-07-27、4 区から 23 区全域へ広げた。境界が都県境と隣接市部まで退がるので
# 上のエッジ効果は周縁へ押しやられる。**ただし供給側の網羅性は区ごとに違う**
# （公共施設一覧を出している区／図書館一覧だけの区／どちらも無い区がある）。
# docs/issues.md A9 を読まずに「到達不可」の区間比較をしてはいけない。
TARGET_WARDS: list[str] = [
    "千代田区", "中央区", "港区", "新宿区", "文京区", "台東区", "墨田区",
    "江東区", "品川区", "目黒区", "大田区", "世田谷区", "渋谷区", "中野区",
    "杉並区", "豊島区", "北区", "荒川区", "板橋区", "練馬区", "足立区",
    "葛飾区", "江戸川区",
]

# 行政区域コード（国土数値情報 N03_007）。
# SHP の DBF が Shift-JIS で読めない環境でも確実に絞り込めるよう、
# 名称とコードの両方で判定する。
# 渋谷区=13113 / 世田谷区=13112 / 新宿区=13104 / 文京区=13105。
TARGET_WARD_CODES: list[str] = [
    "13101", "13102", "13103", "13104", "13105", "13106", "13107",
    "13108", "13109", "13110", "13111", "13112", "13113", "13114",
    "13115", "13116", "13117", "13118", "13119", "13120", "13121",
    "13122", "13123",
]

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
    # 特別支援学校も同じ徒歩圏で扱う。
    #
    # **かつては 1200.0 で、理由は「広域から通学し、送迎車両の往来も周辺に
    # 広がるため長く取る」だった。この理由は読み直すと 1200m を支えていない。**
    # 「広域から通学」が正しいなら、児童生徒は 1200m の輪の中には居ない——
    # 区をまたいでスクールバスで来る。つまりこの輪は通学圏を表しておらず、
    # 45 校の点を広げていただけだった（1200m では 23 区の 63.9% のメッシュが
    # この層の値を持つ。800m なら 38.1%）。送迎車両の往来は負荷側の現象で、
    # 需要側のこの層が測るものでもない。
    #
    # 根拠が持たない数字は、別の数字に置き換えるのではなく**消す**。
    # ここを WALK_BANDWIDTH_M にすると、帯域の自由なパラメータが
    # 3 種（800/1200/600）から 2 種へ減る。新しい恣意性を作らない。
    #
    # **600m（駅と同じ「地点に集中する」扱い）も検討して採らなかった。**
    # そちらは rationale の「同一地点へ集中する」に沿うし、
    # 特別支援学校を外したときの上位 10 件の重なりが 0% → 40% へ改善して
    # この作品の最大の弱点（issues.md A4）が軽くなる。**軽くなるからこそ
    # 採らない。** 結果が有利になる側の値を、新しく作った理由で選んだことに
    # なり、「不利な層だから軽くした」という批判に答えられなくなる
    # （騒音の重みを下げなかったのと同じ判断。issues.md A1）。
    # 800m はこの弱点を全く改善しない（0% のまま）——**結果を見て
    # 選んだのではないことが、結果そのものから分かる側の値である。**
    "sped_school": WALK_BANDWIDTH_M,
    # 駅の過負荷（雑踏・アナウンス・改札）は駅前で急激に減衰する。
    "station_flow": 600.0,
    # 通院は徒歩圏が基本。
    "clinic": WALK_BANDWIDTH_M,
}

# ---------------------------------------------------------------------------
# スコア構成要素
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbsoluteScale:
    """正規化の物差しを「対象地域内の順位」ではなく外部の基準に固定する。

    lo 以下を 0、hi 以上を 1 とし、その間を線形に配分する。

    パーセンタイル正規化は、その層の値がどれだけ狭い範囲に収まっていても
    必ず 0〜1 いっぱいに引き伸ばす。**識別力の無い層ほど差が誇張される**
    という性質があり、実際それで事故を起こした（下の noise を参照）。

    外部の基準に固定すると、層の影響力は「その基準に照らして対象地域が
    実際にどれだけばらついているか」で決まる。影響が小さくなるとしたら、
    それはこちらが重みを下げた結果ではなくデータがそう言っているという
    ことになり、「不利な層だから軽くした」という批判が成り立たなくなる。

    basis には、lo / hi をどの法令・告示から取ったかを書く。
    ここが書けない層に絶対尺度を使ってはいけない（新しい恣意性になる）。
    """

    label: str  # UI 表示用の短い説明
    lo: float  # この値以下を 0 とする
    hi: float  # この値以上を 1 とする
    basis: str  # lo / hi の根拠（法令・告示名）


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

    absolute:
        None 以外を置くと、パーセンタイル正規化を通さず絶対尺度で 0〜1 に
        写す（zero_is_absence は無視される）。AbsoluteScale の説明を参照。
    """

    key: str
    label: str
    side: str
    weight: float
    sign: int
    source: str
    rationale: str
    zero_is_absence: bool = True
    absolute: AbsoluteScale | None = None


DEMAND_COMPONENTS: tuple[Component, ...] = (
    Component(
        key="welfare_capacity",
        label="障害福祉サービス事業所（定員重み）",
        side="demand",
        weight=1.0,
        sign=1,
        source="WAM NET 障害福祉サービス等事業所一覧（届出の種別・実定員）",
        rationale="就労移行・就労継続A/B・生活介護・放課後等デイ等の定員は、"
        "その徒歩圏を日常的に往復せざるを得ない当事者の実数に最も近い代理変数。",
    ),
    Component(
        key="sped_school",
        label="特別支援学校（規模重み）",
        side="demand",
        weight=0.8,
        sign=1,
        source="国土数値情報 P29 学校（位置）＋ 東京都教育委員会 公立学校統計調査"
        "報告書 学校別在籍者数（規模。45 校中 42 校が実数、国立 3 校は規模不明）",
        rationale="通学は選択の余地がない移動であり、児童生徒本人と送迎者の双方が"
        "同一時間帯に同一地点へ集中する。",
    ),
    Component(
        key="station_flow",
        label="駅乗降規模",
        side="demand",
        weight=0.9,
        sign=1,
        source="国土数値情報 S12 駅別乗降客数（2024年値）",
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
        source="国土数値情報 A29 用途地域",
        rationale="本作独自の転用。用途地域は「その土地が法的にどこまで"
        "騒がしくなり得るか」の上限を定めた規制であり、"
        "実測点が疎な感覚負荷を面として推定する予測器として使える。",
        zero_is_absence=False,  # 全メッシュが何らかの用途地域スコアを持つ連続量
        # schema.ZONING_LOAD は用途制限の強さから導いた 0.05〜1.00 の絶対尺度で、
        # 「第二種住居地域でカラオケ・パチンコが解禁される」といった段差を
        # 意図的に置いてある。それをパーセンタイル正規化に通していたため、
        # 段差が均されて第一種低層住居専用地域が 0.05 ではなく 0.285 で乗っていた
        # （設計値の約 6 倍。「静穏が法的に保証された土地」が商業地域の 3 割の
        #  負荷を持つ）。面積加重平均の後も 0〜1 の絶対尺度のままなので、
        # ここでは切り詰めるだけでよい。
        absolute=AbsoluteScale(
            label="用途制限の強さ（第一種低層 0.05 〜 商業 1.00）",
            lo=0.0,
            hi=1.0,
            basis="建築基準法別表第二の用途制限から導いた ZONING_LOAD",
        ),
    ),
    Component(
        key="noise",
        # **「鉄道」を名乗っていたが、鉄道騒音は入っていない。**
        # 都の鉄道騒音調査は測定点が疎で、線路からの距離減衰で補完する設計を
        # 検討したまま入れていない（`SOURCES["tokyo_rail_noise"]` は未使用）。
        # 名前だけ残っていると、画面のこの 1 行が実装より広い範囲を主張する。
        label="自動車騒音（幹線道路の道路端）",
        side="load",
        weight=0.9,
        sign=1,
        source="東京都環境局 自動車交通騒音調査結果（平成25年度・道路端の測定点）",
        rationale="聴覚過敏の直接要因。点測定を距離重み付き内挿で面に変換し、"
        "環境基準に照らした絶対尺度で評価する。",
        zero_is_absence=False,  # 内挿後は全メッシュが騒音レベルを持つ連続量
        # 対象 2 区の内挿値は 62.0〜75.8 dB（標準偏差 2.24 dB、四分位範囲 2.7 dB）に
        # 収まる。音響的には全域がほぼ一様に「幹線道路近接空間の環境基準 70dB」
        # 前後で、地域内では識別力をほとんど持たない。
        #
        # そこへパーセンタイル正規化をかけると 2.24 dB が 0〜1 いっぱい（σ 0.289）へ
        # 引き伸ばされ、実測 2.5 dB しかない区間差が正規化後 0.34 まで拡大していた。
        # 要請限度測定は「苦情の出る道路」を測る調査で測定点の選ばれ方が区に依存する
        # （世田谷区 24 点・渋谷区 5 点）ため、この増幅がそのまま区ダミーとして
        # 働いていた（docs/issues.md A1）。
        #
        # 折れ点は外部の数値を使う。55dB = 騒音に係る環境基準の一般地域 A 類型
        # （昼間）、75dB = 騒音規制法の要請限度（幹線道路近接空間・昼間）。
        # 対象地域が実際どれだけばらついているかがそのまま層の影響力になる。
        absolute=AbsoluteScale(
            label="環境基準 55dB → 0 / 要請限度 75dB → 1",
            lo=55.0,
            hi=75.0,
            basis="騒音に係る環境基準（一般地域A類型・昼間 55dB）と"
            "騒音規制法の要請限度（幹線道路近接空間・昼間 75dB）",
        ),
    ),
    Component(
        key="crowding",
        label="混雑（昼間の従業者数）",
        side="load",
        weight=0.8,
        sign=1,
        source="e-Stat 経済センサス 地域メッシュ統計（従業者数・500m）",
        # 昼間人口そのもののメッシュ統計は配信されていない（国勢調査の
        # 地域メッシュ統計は常住地ベースまで）。「その場所で働いている人の数」で
        # 代替している。買い物客や通学者を含まない一方、自宅に居る人も含まない。
        # 人的密度が生む多重刺激を測るという目的には、後者を含まない方が近い。
        rationale="人的密度そのものが視覚・聴覚・触覚の同時多重刺激を生む。"
        "昼間人口のメッシュ統計は存在しないため、経済センサスの従業者数で代替する。",
    ),
    Component(
        key="green",
        label="緑・公園被覆（減点）",
        side="load",
        weight=0.6,
        sign=-1,
        source="国土数値情報 P13 都市公園（面積相当の円で近似）",
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

# 提言リストを組み立てるときの母数と上限。
#
# **ここが Python と TypeScript で食い違っていた。** 静的な proposals.json は
# 根拠カード（上位 20 区画）から束ねる一方、画面は上位 40 区画から束ねていた。
# 同じ「提言リスト」を名乗りながら母数が違うので、資料に添付した JSON と
# 画面の一覧が別物になり得る。config を唯一の出所にして meta.json 経由で
# 配信する（重みプリセットと同じ扱い）。
PROPOSAL_TOP_N = 40
PROPOSAL_LIMIT = 12

# ---------------------------------------------------------------------------
# 重みプリセット
# ---------------------------------------------------------------------------

# 立場が変われば重みも変わる、ということ自体をツールで示すためのもの。
#
# ここが単一の情報源。meta.json 経由でブラウザへ配信され、
# 感度分析（etl/sensitivity.py）も同じ定義を使う。
# TypeScript 側に重複定義を置くと、画面のプリセットと
# 分析結果が食い違っても誰も気付けない。
PRESETS: tuple[dict, ...] = (
    {
        "id": "default",
        "label": "既定",
        "note": "全レイヤーを均等に近い重みで評価する出発点。",
        "weights": {},  # 空 = Component.weight をそのまま使う
    },
    {
        "id": "auditory",
        "label": "聴覚過敏を重視",
        "note": "音の負荷を最優先。騒音と用途地域の重みを上げ、緑地の減点も強める。",
        "weights": {
            "welfare_capacity": 1.0,
            "sped_school": 0.8,
            "station_flow": 0.9,
            "clinic": 0.5,
            "zoning": 1.2,
            "noise": 1.8,
            "crowding": 0.6,
            "green": 0.9,
        },
    },
    {
        "id": "commute",
        "label": "通所・通学を重視",
        "note": "毎日通わざるを得ない人を優先。乗換ターミナルより日常の生活動線を見る。",
        "weights": {
            "welfare_capacity": 1.8,
            "sped_school": 1.6,
            "station_flow": 0.3,
            "clinic": 0.8,
            "zoning": 0.8,
            "noise": 0.8,
            "crowding": 0.6,
            "green": 0.6,
        },
    },
    {
        "id": "terminal",
        "label": "ターミナル整備を重視",
        "note": "鉄道事業者との連携を前提に、大規模駅の雑踏対策として見る場合。",
        "weights": {
            "welfare_capacity": 0.6,
            "sped_school": 0.3,
            "station_flow": 2.0,
            "clinic": 0.4,
            "zoning": 1.0,
            "noise": 1.0,
            "crowding": 1.4,
            "green": 0.4,
        },
    },
)

# ---------------------------------------------------------------------------
# 感度分析
# ---------------------------------------------------------------------------

# 重みをランダムに揺さぶる幅。0.3 = ±30%。
SENSITIVITY_PERTURBATION = 0.3
# 試行回数。1,116 メッシュ × 8 レイヤーなら 500 回でも一瞬。
SENSITIVITY_TRIALS = 500
# 安定性を測る上位件数。
SENSITIVITY_TOP_K: tuple[int, ...] = (10, 20, 50)

# 重み以外の固定値（仮定員・種別重み・帯域・用途地域の負荷値・α/β・IDW）を
# 揺さぶる試行回数。重みの摂動と違って**集計まで遡って計算し直す**ため
# 1 試行あたり 20〜500 ミリ秒かかり、500 回は現実的でない。
# 群が 7 つあるので、ここを増やすときは全体の実行時間を見て決めること。
FIXED_VALUE_TRIALS = 60

# 帯域を 1 層ずつ掃くときの倍率と試行回数。
# ±30% の乱数より広く取る——**帯域は「徒歩10分 ≒ 800m」という目安から
# 置いた値で、外部の告示に紐づいていない唯一の主要な固定値**なので、
# 「どこまで動かすと結論が変わるか」を範囲で出す（docs/issues.md D1）。
BANDWIDTH_SWEEP: tuple[float, ...] = (0.5, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0)
BANDWIDTH_TRIALS = 40

# 「既存施設では到達不可」を決めている 700m を動かしてみる範囲。
# 優先度は動かない（供給側はスコアの構成要素ではない）が、
# 到達不可という出力そのものはこの 1 つの数字に乗っている。
HOST_DISTANCE_TRIALS: tuple[float, ...] = (500.0, 600.0, 700.0, 800.0, 1000.0)

# 配信 JSON における正規化値の小数桁。
#
# ここで丸めた値がブラウザへ渡り、スライダー操作のたびに再計算される。
# したがって Python 側も「丸めた後の値」から demand/load/priority を計算しなければ、
# 初期表示（Python の計算結果）とスライダーを既定値に戻したとき（ブラウザの計算結果）で
# 色が食い違う。tools/parity_check.mjs がこの一致を検証している。
PUBLISH_DECIMALS = 4

# ---------------------------------------------------------------------------
# 供給側（既存の公共施設）
# ---------------------------------------------------------------------------

# 各メッシュの徒歩圏にある公共施設のうち、**カードに 1 件だけ例示するときの順番**。
# 数値が小さいほど先に選ぶ。
#
# **これは設置先の選定ではない。** かつては「置きやすさの評価」と説明していたが、
# このモデルは施設の適性を測っていないので、そう名乗れる根拠が無かった
# （docs/issues.md A10）。到達可否にも効かない——1 件でも 700m 以内にあれば
# 到達可能なので、この順位を入れ替えても件数もスコアも動かず、
# カードに出る施設名だけが変わる。
HOST_PREFERENCE: dict[str, int] = {
    "図書館": 1,
    "区民センター": 2,
    "出張所": 2,
    "文化施設": 3,
    "児童館": 3,
    "公園管理施設": 4,
    "駅": 5,
}

# 徒歩圏とみなす距離。この中に 1 件も無ければ
# 「既存ストックの徒歩圏から外れている＝新規整備か民間施設との連携が要る」と述べる。
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

    # --- ここから下は「画面に出すため」の欄 ---
    #
    # レジストリに載っているだけでは、その出典を実際に使ったことにならない。
    # 実際 16 件のうち 5 件は検討しただけで使っていない（ODPT・不動産情報
    # ライブラリ・鉄道騒音・手帳交付状況・既存スペース）。**画面が出典一覧を
    # 出すなら、使っていないものを混ぜて数を稼いではいけない。**
    layer: str | None = None  # 生成に使ったレイヤー名。None = 未使用
    vintage: str = ""  # 年次。年がそろっていないこと自体が課題（issues.md C1）


SOURCES: dict[str, Source] = {
    # --- 対象地域そのもの ---
    # **画面の出典一覧から抜けていた。** メッシュを張る範囲も、提言の見出しの
    # 区名も、到達不可の区ごとの集計もこのレイヤーで決まる。
    # 「地図の下敷きだから出典ではない」ということはない。
    "ksj_n03_boundary": Source(
        key="ksj_n03_boundary",
        label="国土数値情報 N03 行政区域",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-v3_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="メッシュを張る範囲・区名・区ごとの集計の基準。"
        "名称とコード（N03_007）の両方で対象 23 区を判定する。",
        layer="area",
        vintage="2024年",
    ),
    # --- 需要レイヤー ---
    "wamnet_jigyosho": Source(
        key="wamnet_jigyosho",
        label="障害福祉サービス等事業所一覧（所在地・定員）",
        url="https://www.wam.go.jp/content/wamnet/pcpub/top/sfkopendata/",
        kind="csv",
        license="WAM NET 二次利用可（出典表示）",
        note="サービス種別ごとに 29 分割された全国 CSV（都道府県別ではない）。"
        "事業所緯度・経度を持つのでジオコーディングは不要。",
        layer="welfare",
        vintage="2026年3月",
    ),
    "ksj_p29_school": Source(
        key="ksj_p29_school",
        label="国土数値情報 P29 学校（特別支援学校を含む）",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P29.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="P29_003 学校分類コード 16012 = 特別支援学校。"
        "**在籍者数は持たない**ので規模は tokyo_sped_enrollment から当てる。",
        layer="schools",
        vintage="2023年度",
    ),
    "tokyo_sped_enrollment": Source(
        key="tokyo_sped_enrollment",
        label="東京都教育委員会 公立学校統計調査報告書（学校別在籍者数）",
        url="https://www.kyoiku.metro.tokyo.lg.jp/about/statistics_and_research"
        "/list_of_public_school/school_lists2025/report2025_csv",
        kind="csv",
        license="東京都 オープンデータ（出典表示）",
        note="令和7年度・5月1日現在。**公立のみ**が対象で国立・私立は載らない。"
        "1 行 = 学校 × 障害種別で、併置校は同じ学校番号が複数行に分かれる。",
        layer="schools",
        vintage="令和7年度（2025年5月1日）",
    ),
    "ksj_p14_welfare": Source(
        key="ksj_p14_welfare",
        label="国土数値情報 P14 福祉施設（定員つき）",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P14-v2_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="需要側は WAM NET へ置き換えた。現在はホスト施設（児童館）の出典。",
        layer="hosts",
        vintage="2022年度",
    ),
    "ksj_p04_medical": Source(
        key="ksj_p04_medical",
        label="国土数値情報 P04 医療機関",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P04-v3_1.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="診療科目欄から精神科・心療内科を抽出する。",
        layer="clinics",
        vintage="2020年度",
    ),
    "ksj_s12_station": Source(
        key="ksj_s12_station",
        label="国土数値情報 S12 駅別乗降客数",
        url="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-S12-2024.html",
        kind="shp",
        license="国土数値情報 利用約款（出典表示）",
        note="ODPT の代替。アクセストークンが要らず、乗降客数が年次で入る。"
        "1 行が「駅×事業者×路線」なのでグループコードで束ねて合算する。",
        layer="stations",
        vintage="2024年値",
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
        note="reinfolib_youto の代替。用途地域コードは A29-19_13 では A29_004"
        "（列名は年度で変わるため中身から自動判定する）。",
        layer="zoning",
        vintage="2019年度",
    ),
    "tokyo_road_noise": Source(
        key="tokyo_road_noise",
        label="自動車騒音 要請限度測定結果",
        url="https://catalog.data.metro.tokyo.lg.jp/dataset/t000010d0000000041",
        kind="csv",
        license="東京都オープンデータカタログ CC BY 4.0",
        note="測定地点の点データ。等価騒音レベル LAeq を距離重み付き内挿する。",
        layer="noise",
        vintage="平成25年度（2013）",
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
        layer="parks",
        vintage="2011年度",
    ),
    "estat_mesh_pop": Source(
        key="estat_mesh_pop",
        label="地域メッシュ統計 人口・昼間人口",
        url="https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData",
        kind="api",
        license="政府統計 e-Stat 利用規約（出典表示）",
        api_key_env="ESTAT_APP_ID",
        note="メッシュコードで直接結合できる唯一のレイヤー。空間補間が不要。",
        layer="population",
        vintage="2021年 経済センサス",
    ),
    # --- 供給側 ---
    "tokyo_public_facility": Source(
        key="tokyo_public_facility",
        label="公共施設一覧（図書館・文化施設・区民センター等）",
        url="https://catalog.data.metro.tokyo.lg.jp/dataset?q=公共施設",
        kind="csv",
        license="東京都オープンデータカタログ CC BY 4.0",
        note="区市町村ごとに様式が異なるため normalize 側で名寄せする。",
        layer="hosts",
        vintage="2025年時点で各区が公開",
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
