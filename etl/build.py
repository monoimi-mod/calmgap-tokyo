"""
パイプライン全体のオーケストレータ。

    python -m etl.build              # 模擬データで実行（既定・ネットワーク不要）
    python -m etl.build --live       # 実オープンデータで実行
    python -m etl.build --level 4    # 4次メッシュ(500m)で実行
    python -m etl.build --report     # 上位メッシュと相関の点検表を表示

出力は web/public/data/ 配下:
    mesh.geojson       メッシュ形状 + 正規化済み構成要素 + 既定重みでのスコア
    cards.json         上位メッシュの根拠カード
    proposals.json     提言リスト（＝優先度の順位表）。**単位は区画**
                       （地区でも施設でもない。理由は etl/hosts.py の build_ranking）
    hosts.geojson      既存の公共施設（供給側）
    demand_points.geojson  需要側の点データ（地図の文脈表示用）
    noise_points.geojson   騒音の測定地点（施設ではない。調査が測った場所）
    parks.geojson      公園（緑・公園被覆を作っているもの。退避先ではない）
    meta.json          構成要素定義・出典・生成条件

重みの掛け合わせは意図的にブラウザ側へ残してある。
ここで出力するのは「正規化済みの素材」であり、
最終スコアはユーザーがスライダーで動かした結果として決まる。
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import aggregate, fixtures, hosts as hostlib, mesh as meshlib, score, sensitivity
from .schema import ZONING_NAME
from .config import (
    A1_TRACKED_PAIR,
    ALL_COMPONENTS,
    BANDWIDTH_M,
    DEMAND_COMPONENTS,
    HOST_MAX_DISTANCE_M,
    LOAD_COMPONENTS,
    MESH_LEVEL,
    NOISE_IDW_MAX_DISTANCE_M,
    PRIORITY_ALPHA,
    PRIORITY_BETA,
    PRESETS,
    RANKING_DEFAULT_N,
    RANKING_OPTIONS,
    CRS_GEOGRAPHIC,
    current_source_label,
    CRS_PROJECTED,
    PUBLISH_XY_DECIMALS,
    SOURCES,
    SOURCE_JOIN,
    STUDY_BBOX,
    SUPPORT_LAYERS,
    TARGET_WARDS,
    TOP_N_CARDS,
    WEB_DATA,
)

# 「最寄り駅」を探す上限距離。これを超えると駅名を出さない。
#
# 件数ではなく最寄り 1 件を採る唯一の実数で、他の f_* と半径の意味が違う。
# 画面がこの値を書けるよう meta.json へ出す（TypeScript に直接書くと、
# ここを動かしたときに画面のラベルだけが古くなる）。
STATION_MAX_DISTANCE_M = 1500.0


# ---------------------------------------------------------------------------
# レイヤー読み込み
# ---------------------------------------------------------------------------


def load_layers(live: bool) -> tuple[dict, dict[str, str]]:
    """全レイヤーを読み込む。live=False なら全て模擬データ。

    live=True のとき、data/processed にある実データで模擬データを
    **1 レイヤーずつ差し替える**。オープンデータは 1 本ずつしか片付かないので、
    全部そろうまで地図が動かないより、落とせた分から反映できる方が作業が進む。

    Returns
    -------
    (レイヤー辞書, レイヤー名 → "real" | "synthetic" の対応)
    """
    layers = fixtures.generate_all()
    provenance = {k: "synthetic" for k in layers if not k.startswith("_")}
    # 昼間人口だけはメッシュが確定してからでないと模擬データを作れないため
    # fixtures.generate_all に入っていない。**数え漏らすと、混雑レイヤーが
    # 乱数のままなのに「実データ 9/9」と表示されて警告バナーが消える。**
    # 生成の都合でレイヤーが 1 つ数から外れる、という壊れ方をさせない。
    provenance["population"] = "synthetic"

    if not live:
        return layers, provenance

    from . import fetch

    real = fetch.load_processed()
    for key, gdf in real.items():
        if gdf is None or len(gdf) == 0:
            print(f"[live] {key}: 実データが空のため模擬データを使う")
            continue
        layers[key] = gdf
        provenance[key] = "real"

    n_real = sum(1 for v in provenance.values() if v == "real")
    print(f"\n[live] 実データ {n_real}/{len(provenance)} レイヤー")
    for key in sorted(provenance):
        mark = "実データ" if provenance[key] == "real" else "模擬  "
        n = len(layers[key]) if hasattr(layers.get(key), "__len__") else 0
        note = "（メッシュ確定後に生成）" if key == "population" and not n else ""
        print(f"       {mark}  {key:<10} {n:>6,d}件{note}")
    if n_real < len(provenance):
        print("       残りは data/processed に置けば自動で切り替わる:")
        print("       python -m etl.fetch --normalize <種別> <ファイル>\n")

    return layers, provenance


# ---------------------------------------------------------------------------
# メッシュへの集計
# ---------------------------------------------------------------------------


def build_mesh_table(layers: dict, level: int) -> gpd.GeoDataFrame:
    """研究領域のメッシュを作り、全構成要素の生値を列として付ける。"""
    area = layers.get("area")

    # メッシュを張る矩形は行政界から導出する。余白は取らない
    # （入力データの絞り込みだけが余白付き。config.CLIP_BUFFER_M 参照）。
    # 暫定矩形のままだと対象2区の外まで広がり、渋谷区の北端も切れていた。
    if area is not None and len(area):
        bbox = tuple(float(v) for v in area.to_crs(CRS_GEOGRAPHIC).total_bounds)
    else:
        bbox = STUDY_BBOX

    mesh_gdf = aggregate.build_mesh_frame(bbox, level, clip=area)
    ny, nx = meshlib.cell_size_meters(level)
    print(
        f"[mesh] {meshlib.LEVEL_LABEL[level]} / {len(mesh_gdf):,} セル "
        f"(1セル 約 {ny:.0f}m × {nx:.0f}m)\n"
        f"       範囲 ({bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f})"
    )

    # --- 需要側 ---
    mesh_gdf["welfare_capacity"] = aggregate.points_to_mesh(
        mesh_gdf,
        layers["welfare"],
        "demand_value",
        BANDWIDTH_M["welfare_capacity"],
    ).to_numpy()

    mesh_gdf["sped_school"] = aggregate.points_to_mesh(
        mesh_gdf, layers["schools"], "demand_value", BANDWIDTH_M["sped_school"]
    ).to_numpy()

    mesh_gdf["station_flow"] = aggregate.points_to_mesh(
        mesh_gdf, layers["stations"], "demand_value", BANDWIDTH_M["station_flow"]
    ).to_numpy()

    mesh_gdf["clinic"] = aggregate.points_to_mesh(
        mesh_gdf, layers["clinics"], "demand_value", BANDWIDTH_M["clinic"]
    ).to_numpy()

    # --- 負荷側 ---
    mesh_gdf["zoning"] = aggregate.polygon_area_weighted_mean(
        mesh_gdf, layers["zoning"], "zoning_load", default=0.30
    ).to_numpy()

    noise = aggregate.idw_to_mesh(mesh_gdf, layers["noise"], "laeq_db")
    # 内挿の届かないメッシュは「観測なし」。0 で埋めると
    # 最も静かな場所として最上位に評価されてしまうため中央値で補完する。
    n_missing = int(noise.isna().sum())
    if n_missing:
        print(f"[noise] 観測圏外 {n_missing} セルを中央値で補完")
    mesh_gdf["noise"] = noise.fillna(noise.median()).to_numpy()

    # 昼間人口はメッシュコードで直接結合できる唯一のレイヤー。
    # 模擬モードではメッシュ確定後でないと作れないため、ここで生成する。
    pop = layers.get("population")
    if pop is None:
        pop = fixtures.daytime_population(
            np.random.default_rng(7), mesh_gdf["mesh_code"].tolist()
        )
    mesh_gdf["crowding"] = aggregate.join_by_mesh_code(
        mesh_gdf, pop, "mesh_code", "daytime_population"
    ).to_numpy()

    mesh_gdf["green"] = aggregate.polygon_coverage_ratio(
        mesh_gdf, layers["parks"]
    ).to_numpy()

    # メッシュ自身の区名。**表示だけの列ではない。** 提言の宛先は施設ではなく
    # 区であり、地区の見出しにも区名が要る。以前は割り当てた施設の区名で
    # 代用していたが、それは「この区画がどの区か」ではなく「例示した施設が
    # どの区か」で、区境際では食い違う。面積最大の区を採る（重心だと
    # 行政界で切られたセルの重心が区外に出ることがある）。
    mesh_gdf["ward"] = aggregate.polygon_dominant_class(
        mesh_gdf, area, "ward"
    ).to_numpy()

    _attach_facts(mesh_gdf, layers)
    _check_noise_point_count(mesh_gdf, noise)
    return mesh_gdf


def _check_noise_point_count(mesh_gdf: gpd.GeoDataFrame, noise: pd.Series) -> None:
    """「内挿に使った測定点 0 件」と「観測圏外」が同じ区画であることを確かめる。

    画面は騒音の行に「内挿に使った測定点 N 点」と書き、その N 点を光らせる。
    **N が 0 のとき、その区画の騒音は測定ではなく 23 区の中央値である**——
    画面はそう書く。だからこの 2 つが同じ区画集合でなければ、画面は
    「補完値です」と書きながら測定点を光らせる（またはその逆）ことになる。

    ズレ得る経路は 1 つだけで、座標の丸めである。IDW は丸めていない投影座標で
    距離を測り、`f_noise_n` は**配信するのと同じ丸めた座標**で数える
    （画面と一致させるため。CLAUDE.md「数えたものと光らせるもの」）。
    打ち切り 1,500m のちょうど境界に 1cm 以内で載る測定点があれば食い違う。
    **黙って通すと、画面のその 1 区画だけが嘘になる。**
    """
    missing = noise.isna().to_numpy()
    counted_zero = mesh_gdf["f_noise_n"].to_numpy() == 0
    bad = np.nonzero(missing != counted_zero)[0]
    if len(bad):
        codes = ", ".join(mesh_gdf["mesh_code"].to_numpy()[bad][:5])
        raise ValueError(
            f"騒音: 観測圏外の区画（{int(missing.sum())} 件）と、"
            f"打ち切り {NOISE_IDW_MAX_DISTANCE_M:.0f}m 以内の測定点が 0 件の区画"
            f"（{int(counted_zero.sum())} 件）が一致しない: {len(bad)} 区画（例 {codes}）。"
            "打ち切り距離のちょうど境界に測定点が載っている。"
            "config.PUBLISH_XY_DECIMALS の丸めが原因なので、"
            "**画面の件数と補完の説明のどちらを直すかを決めてから**進めること。"
        )


def _attach_facts(mesh_gdf: gpd.GeoDataFrame, layers: dict) -> None:
    """根拠カードに載せる「実数」を付ける。

    スコアは順位に正規化された相対値であり、それ単体では提言文にならない。
    「需要 0.94」ではなく「徒歩圏に事業所 7 件・定員 138 人、
    最寄りは渋谷駅で乗降 62 万人」と書けて初めて予算会議の資料になる。
    スコア計算には一切使わず、表示専用の列として持つ（接頭辞 f_）。
    """
    walk = BANDWIDTH_M["welfare_capacity"]

    # round_decimals は配信する座標と揃える。**画面がこの件数の点を
    # 地図に光らせるため**、両者が別の座標で距離を測ってはいけない。
    r = PUBLISH_XY_DECIMALS
    mesh_gdf["f_welfare_n"] = aggregate.count_within(
        mesh_gdf, layers["welfare"], walk, round_decimals=r
    ).to_numpy()
    mesh_gdf["f_welfare_cap"] = aggregate.count_within(
        mesh_gdf, layers["welfare"], walk, "capacity", round_decimals=r
    ).to_numpy()
    mesh_gdf["f_school_n"] = aggregate.count_within(
        mesh_gdf, layers["schools"], BANDWIDTH_M["sped_school"], round_decimals=r
    ).to_numpy()
    mesh_gdf["f_clinic_n"] = aggregate.count_within(
        mesh_gdf, layers["clinics"], walk, round_decimals=r
    ).to_numpy()

    # 徒歩圏の事業所のうち、対象 23 区の外にあるものの件数。
    #
    # 入力は区界の外側 2km まで拾う設計で（config.CLIP_BUFFER_M）、これは
    # エッジ効果を避けるために正しい。**問題は、その事実が根拠カードに
    # 出ていなかったこと**（docs/issues.md B1）。障害福祉事業所は
    # 14,118 件のうち 5,195 件（36.8%）が区外——WAM NET が都全域の届出を
    # 含み、多摩地域の事業所がバッファに入るため。外周のメッシュは
    # 「隣の市の事業所」で需要が決まっていることがあり得るのに、
    # カードは徒歩圏の総数しか出していなかった。
    outside = _outside_target_wards(layers["welfare"], layers.get("area"))
    mesh_gdf["f_welfare_outside_n"] = aggregate.count_within(
        mesh_gdf, layers["welfare"][outside], walk
    ).to_numpy()

    # 供給側の実数。**これが供給側について言える唯一のこと**——徒歩圏に
    # 屋内の公共空間が幾つ在るか。どれが適するかは測っていない。
    # 数えているのは施設一覧の行数であって建物の数ではない（100m 以内に
    # 別種別の行が並ぶ組が残っている。docs/issues.md）。
    mesh_gdf["f_host_n"] = aggregate.count_within(
        mesh_gdf, layers["hosts"], HOST_MAX_DISTANCE_M, round_decimals=r
    ).to_numpy()

    # **駅も件数で数える。他の需要 3 層と同じく、半径はその層の帯域。**
    #
    # かつてここは「1,500m 以内の最寄り 1 駅」しか出していなかった。
    # だが**スコアは最寄り 1 駅など見ていない**——`station_flow` は他の層と
    # 同じカーネル集計で、帯域 600m の重みで**周囲の全駅を足している**。
    # 画面だけが 1 駅を指しており、
    #
    #   - 事業所 60 件・学校 1 校…と件数が並ぶ中で駅だけ 1 件に見える
    #   - 駅が 2 つ以上あって人が多い区画を、画面が表現できない
    #     （実際 600m 以内に 2 駅以上ある区画が 1,992 件・21.0% ある）
    #   - 1,500m という半径がスコアのどの数字とも対応しない
    #     （帯域 600m でもカットオフ 1,200m でもない）
    #
    # という 3 つが同時に起きていた。**表示半径はその層の帯域に揃える**
    # （事業所・学校・クリニックが 800m を出しているのと同じ規則）。
    walk_station = BANDWIDTH_M["station_flow"]
    mesh_gdf["f_station_n"] = aggregate.count_within(
        mesh_gdf, layers["stations"], walk_station, round_decimals=r
    ).to_numpy()
    mesh_gdf["f_station_sum"] = aggregate.count_within(
        mesh_gdf, layers["stations"], walk_station, "capacity", round_decimals=r
    ).to_numpy()

    # **最寄り 1 駅は残す。役割が違う**——順位表の見出し「豊島区 大塚」の
    # 「大塚」がこれで、**区画の呼び名**である。需要の実数ではない。
    # 半径 1,500m はそのための到達範囲で、これを帯域に揃えて 600m にすると
    # 9,507 区画のうち 4,197 件（44.1%）が呼び名を失う。
    station = aggregate.nearest_feature(
        mesh_gdf,
        layers["stations"],
        ["name", "capacity"],
        max_distance_m=STATION_MAX_DISTANCE_M,
    )
    mesh_gdf["f_station_name"] = station["name"].to_numpy()
    mesh_gdf["f_station_riders"] = station["capacity"].to_numpy()
    mesh_gdf["f_station_dist"] = station["distance_m"].to_numpy()

    zoning_code = aggregate.polygon_dominant_class(
        mesh_gdf, layers["zoning"], "zoning_code"
    )
    mesh_gdf["f_zoning_name"] = [
        ZONING_NAME.get(int(c)) if pd.notna(c) else None for c in zoning_code
    ]

    # 騒音と緑被覆は生値がそのまま意味を持つので、丸めるだけ。
    mesh_gdf["f_noise_db"] = np.round(mesh_gdf["noise"].to_numpy(), 1)

    # **内挿に使った測定点の件数。** 他の f_*_n と違い「徒歩圏」ではなく
    # IDW の打ち切り距離（1,500m）で数える——半径の意味が層ごとに違うのは
    # 駅（帯域）・公共施設（到達可否の境目）と同じで、**その層の計算が
    # 実際に見ている範囲**に揃えるという規則のほう。
    #
    # **0 件は「静か」ではなく「測っていない」。** その区画の騒音は
    # 23 区の中央値で補完されており、画面はそれをこの件数で言う
    #（`docs/issues.md` A6）。数字を出すだけでなく点を光らせられるのは、
    # 「測定点が幹線道路の道路端にしか無い」という A1 の偏りが、
    # 説明ではなく地図で見えるようにするため。
    mesh_gdf["f_noise_n"] = aggregate.count_within(
        mesh_gdf, layers["noise"], NOISE_IDW_MAX_DISTANCE_M, round_decimals=r
    ).to_numpy()
    mesh_gdf["f_green_pct"] = np.round(mesh_gdf["green"].to_numpy() * 100, 1)
    # **被覆率を作った公園そのものを画面へ運ぶ。** 8 層のうち、実数の隣に
    # 「その数を作ったもの」を出せないのはこの層だけだった——「緑・公園被覆
    # 0%」と書いてあっても、**近くに公園が本当に無いのか、隣の区画に
    # 寄っているだけなのかが画面から分からない。**
    #
    # **半径ではなく重なりで数える。** 他の需要 4 層は帯域と同じ半径の
    # 円で数えるが、この層は区画そのものの被覆率なので、数えるべきは
    # 「この区画に重なる公園」である。**揃っていないのは規則が違うから**で、
    # 揃えると被覆率と一覧が別のものを指すことになる。
    #
    # **ブラウザに数え直させない。** 円（`normalize_parks` が面積から作る）と
    # セル形状の交差を矩形近似で再現すると、**9,507 区画のうち 9 区画で
    # 件数が食い違った**（うち 6 区画は 0 件かどうかまで変わる）。投影後の
    # セルは軸に平行な矩形ではなく、経度で 0.6m ほど傾くためである。
    # 対応表そのものを配れば、食い違う余地が構造的に無い。
    # **`.to_numpy()` を通す。** 返り値は mesh_code を索引にした Series で、
    # そのまま代入すると RangeIndex の側と突き合わされて全行 NaN になる
    #（他の集計も同じ理由で全部 to_numpy している）。
    green_parks = aggregate.polygon_overlap_index(mesh_gdf, layers["parks"]).to_numpy()
    mesh_gdf["green_parks"] = green_parks
    mesh_gdf["f_green_n"] = np.array([len(v) for v in green_parks], dtype=int)
    # 混雑だけ実数を配信していなかった。**8 層のうち 1 層だけ実数が無いと、
    # 「正規化値 0.93」の隣に何も置けない**——画面は実数と正規化値を
    # 1 行に並べる作りにしたので、そこが空くと対応関係の説明が崩れる。
    mesh_gdf["f_crowding"] = np.round(mesh_gdf["crowding"].to_numpy()).astype(int)


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def _feature_properties(row: pd.Series, xy: tuple[float, float] | None = None) -> dict:
    """配信サイズを抑えるため、必要な列だけを丸めて出す。"""
    props = {"c": row["mesh_code"]}
    # 平面直角座標系（EPSG:6677）での重心。**Python が徒歩圏の件数を
    # 数えているのと同じ座標**を配ることで、画面が「この 60 件」を
    # 光らせたときに表の数字とズレない（`_write_geojson` の with_xy 参照）。
    if xy is not None:
        props["mx"] = round(xy[0], PUBLISH_XY_DECIMALS)
        props["my"] = round(xy[1], PUBLISH_XY_DECIMALS)
    # 区名は文字列で持つと 9,507 件で無視できない量になるので、
    # meta.json の target_wards への添字で渡す。一覧に無い区名なら省く
    # （ブラウザ側は区名なしとして地区の見出しを組む）。
    ward = str(row.get("ward") or "")
    if ward in TARGET_WARDS:
        props["w"] = TARGET_WARDS.index(ward)
    for comp in ALL_COMPONENTS:
        props[f"n_{comp.key}"] = score.publish_round(row[f"n_{comp.key}"])
    for key in ("demand", "load", "priority"):
        props[key] = score.publish_round(row[key])
    if row.get("host_name"):
        props["host"] = row["host_name"]
        props["host_kind"] = row["host_kind"]
        if row.get("host_ward"):
            props["host_ward"] = row["host_ward"]
        if pd.notna(row.get("host_distance_m")):
            props["host_d"] = int(row["host_distance_m"])

    # 根拠カード用の実数（スコアには使わない）。
    # 0 や欠損は載せない — 「事業所 0 件」を書き出しても提言文に使わないため、
    # 全メッシュ分で見ると無駄が大きい。
    for key, cast in (
        ("f_welfare_n", int),
        ("f_welfare_cap", int),
        ("f_welfare_outside_n", int),
        ("f_school_n", int),
        ("f_clinic_n", int),
        ("f_station_n", int),
        ("f_station_sum", int),
        ("f_station_riders", int),
        ("f_station_dist", int),
        ("f_host_n", int),
        ("f_noise_n", int),
        ("f_crowding", int),
        ("f_noise_db", float),
        ("f_green_pct", float),
        ("f_green_n", int),
    ):
        v = row.get(key)
        if pd.notna(v) and float(v) != 0.0:
            props[key] = cast(v)
    # この区画に重なる公園の添字（parks.geojson の行順）。
    # **件数ではなく対応表そのものを配る**——ブラウザが数え直すと
    # 9 区画で食い違う（`_attach_facts` の green_parks 参照）。
    # 空のときは載せない（4,192 区画にしか付かない）。
    gp = row.get("green_parks")
    if isinstance(gp, (list, tuple)) and len(gp):
        props["gp"] = list(gp)
    for key in ("f_station_name", "f_zoning_name"):
        v = row.get(key)
        if v is not None and pd.notna(v):
            props[key] = str(v)
    return props


def _outside_target_wards(points: gpd.GeoDataFrame, area) -> np.ndarray:
    """対象区の外にある点を True で返す。

    行政界が無いとき（模擬モードで area も模擬のとき）は全て False を返す。
    **「区外は 0 件」と表示されるが、それは事実**——模擬データは対象矩形の
    中だけに生成されるので、区外の施設という概念が無い。
    """
    if area is None or not len(area) or points is None or not len(points):
        return np.zeros(len(points) if points is not None else 0, dtype=bool)
    inside = gpd.sjoin(
        points[["geometry"]].to_crs(CRS_GEOGRAPHIC),
        area[["geometry"]].to_crs(CRS_GEOGRAPHIC),
        how="left",
        predicate="within",
    )
    # sjoin は 1 点が複数ポリゴンに当たると行が増える。点の index で畳む。
    hit = inside.groupby(level=0)["index_right"].apply(lambda s: s.notna().any())
    return ~hit.reindex(points.index, fill_value=False).to_numpy()


def _unreachable_summary(scored: pd.DataFrame) -> dict:
    """到達不可の要約。画面の見出し数値になる。

    **区ごとの内訳は配信しない。** 到達不可率は非市街地の面積比でほぼ決まり
    （江東区 38.7% は埋立地、大田区 38.2% は羽田空港、千代田区 28.2% は皇居）、
    区の間で並べてよい数字ではない。配信すると必ず並べられる。
    区をまたいで比べてよいのは mid_or_above の側だけ（`hosts.reach_report`）。
    """
    reach = hostlib.reach_report(scored)
    n = int((scored["f_host_n"].fillna(0).astype(float) == 0).sum())
    return {
        "count": n,
        "ratio": round(n / len(scored), 4) if len(scored) else 0.0,
        "mid_or_above": int(reach["中位以上"].sum()),
        "note": "区内で優先度が中位以上のもの。既定重みでの値で、"
        "区をまたいで並べてよいのはこちら。",
    }


CALM_SPACES_PATH = Path(__file__).resolve().parent.parent / "data/reference/calm_spaces.json"


def _calm_space_report(scored: pd.DataFrame) -> dict | None:
    """既に置かれているカームダウンスペースを、この方法の順位と突き合わせる。

    **この作品で唯一の外部照合である。** `parity_check` も `selftest` も
    `doc_numbers` も内部整合しか見ておらず、**モデルが現実を当てている
    証拠にはならない。** 既存の設置場所と比べて初めて、外から確かめたことになる
    （`docs/methodology.md` 8.3）。

    **どちらに転んでも発見になる設計。** 上位に寄っていればモデルが
    実務家の判断を再現できているという主張になり、ズレていれば
    「現在の配置がニーズと合っていない」という提言そのものになる。

    **入力は手で集めた一覧で、他のレイヤーとは出自が違う**
    （`data/reference/calm_spaces.json`。中央集約されたオープンデータが
    無いため）。**網羅性の保証は無い**ので、スコアには一切入れない。

    **`access` を必ず一緒に数える。** 室数だけを数えると
    「23 区に 16 室ある」と読めるが、**その大半は施設の中にあり、
    その施設の利用者しか使えない**——大学は学生・教職員、空港は保安検査後、
    博物館は入館料が要る。**街を歩いている人がその場で使えるか**が、
    この地図が前提にしている到達可否である。
    """
    if not CALM_SPACES_PATH.exists():
        return None
    doc = json.loads(CALM_SPACES_PATH.read_text(encoding="utf-8"))
    sites = doc.get("sites", [])
    if not sites:
        return None

    # 優先度の順位（1 が最上位）。scored の並びに依存させない。
    order = scored["priority"].to_numpy().argsort()[::-1]
    rank_of: dict[str, int] = {}
    codes = scored["mesh_code"].tolist()
    for r, i in enumerate(order):
        rank_of[codes[i]] = r + 1
    host_of = dict(zip(scored["mesh_code"], scored["f_host_n"].fillna(0).astype(int)))

    rows = []
    for s in sites:
        code = meshlib.encode(float(s["lat"]), float(s["lon"]), 5)
        rows.append(
            {
                "name": s["name"],
                "rooms": int(s["rooms"]),
                "access": s["access"],
                "mesh_code": code,
                # 対象地域の外に出ることは無いはずだが、出たら黙って 0 にしない。
                "rank": rank_of.get(code),
                "host_n": host_of.get(code),
            }
        )

    ranked = sorted(r["rank"] for r in rows if r["rank"] is not None)
    n_mesh = len(scored)
    by_access: dict[str, int] = {}
    for r in rows:
        by_access[r["access"]] = by_access.get(r["access"], 0) + r["rooms"]

    return {
        "surveyed_at": doc.get("surveyed_at", ""),
        "sites": rows,
        "site_count": len(rows),
        "room_count": sum(r["rooms"] for r in rows),
        # **「誰でも使える」室数を別に出す。** ここがこの照合の要点で、
        # 室数だけでは「16 室ある」と読めてしまう。
        "rooms_by_access": by_access,
        "open_rooms": by_access.get("open", 0),
        "rank_min": ranked[0] if ranked else None,
        "rank_median": int(np.median(ranked)) if ranked else None,
        "rank_max": ranked[-1] if ranked else None,
        "in_top_50": sum(1 for r in ranked if r <= 50),
        "in_bottom_half": sum(1 for r in ranked if r > n_mesh / 2),
        "mesh_count": n_mesh,
        "note": "手で集めた一覧との照合。網羅性の保証は無く、スコアには入らない。",
    }


def _ward_dependence_summary(scored: pd.DataFrame) -> dict:
    """区ダミーとの相関の要約（`docs/issues.md` A1 の診断指標）。

    **層ごとに「絶対値が最大の区」だけを載せる。** 全 8 層 × 23 区の
    行列を配信すると、区の間で並べられる形になる——到達不可の内訳を
    配信しないのと同じ理由で（`_unreachable_summary`）、ここで知りたいのは
    「その層が 1 つの区に張り付いていないか」であって区の順位ではない。

    `noise_ward_mean` だけは値の側も載せる。相関は「どの区に紐づくか」
    しか答えず、**「世田谷区の内挿平均 68.7dB は 23 区で最も高い」**
    という主張には平均そのものが要るため。騒音に限るのは、この層だけが
    測定設計の偏りを既知の問題として抱えているから（A1・A6）。
    """
    table = score.ward_dependence_report(scored)
    components = [
        {
            "key": row["key"],
            "label": row["構成要素"],
            "ward": row["ward"],
            "corr": float(row["相関"]),
        }
        for _, row in table.iterrows()
    ]

    summary: dict = {
        "components": components,
        "note": "区ダミーとの相関。層ごとに絶対値が最大の区。"
        "0 から離れるほど、その層は実質その区のダミーとして働いている。",
    }

    # A1 が 5 ビルド並べてきた組（騒音 × 世田谷区）。**決め打ちだが要る**
    # ——系列として比べる以上、途中で見る区を変えたら比較にならない
    #（score.ward_dummy_correlation のコメント）。
    tracked_corr = score.ward_dummy_correlation(scored, *A1_TRACKED_PAIR)
    if tracked_corr is not None:
        summary["tracked"] = {
            "key": A1_TRACKED_PAIR[0],
            "ward": A1_TRACKED_PAIR[1],
            "corr": round(tracked_corr, 4),
            "note": "docs/issues.md A1 が経緯を並べてきた組。区を決め打ちして"
            "いるのは系列を比較するためで、偏りの現在地は components を見ること。",
        }

    spread = score.ward_value_spread(scored, "f_noise_db")
    if len(spread) >= 2:
        lo, hi = spread.iloc[0], spread.iloc[-1]
        summary["noise_ward_mean"] = {
            "min_ward": lo["区"],
            "min_db": float(lo["f_noise_db"]),
            "max_ward": hi["区"],
            "max_db": float(hi["f_noise_db"]),
            "spread_db": round(float(hi["f_noise_db"]) - float(lo["f_noise_db"]), 1),
        }
    return summary


def write_outputs(
    mesh_gdf: gpd.GeoDataFrame,
    scored: pd.DataFrame,
    layers: dict,
    cards: list[dict],
    proposals: list[dict],
    live: bool,
    level: int,
    provenance: dict[str, str],
    generated_at: str,
) -> None:
    WEB_DATA.mkdir(parents=True, exist_ok=True)

    # --- mesh.geojson ---
    # 重心は投影座標で取る。aggregate.count_within が使っているものと同じ。
    centroids = mesh_gdf.to_crs(CRS_PROJECTED).geometry.centroid
    features = [
        {
            "type": "Feature",
            "geometry": _round_geometry(geom),
            "properties": _feature_properties(row, (float(c.x), float(c.y))),
        }
        for geom, (_, row), c in zip(
            mesh_gdf.geometry.map(lambda g: g.__geo_interface__),
            scored.iterrows(),
            centroids,
        )
    ]
    _write_json(
        WEB_DATA / "mesh.geojson", {"type": "FeatureCollection", "features": features}
    )

    # --- 供給側・需要側の点 ---
    _write_geojson(
        WEB_DATA / "hosts.geojson",
        layers["hosts"],
        ["name", "host_kind", "source"],
        with_xy=True,
    )

    demand_points = pd.concat(
        [
            layers["welfare"].assign(layer="welfare"),
            layers["schools"].assign(layer="school"),
            layers["stations"].assign(layer="station"),
            layers["clinics"].assign(layer="clinic"),
        ],
        ignore_index=True,
    )

    # **「規模」の隣に、それが実測か仮置きかを必ず添える。**
    # 地図の点をクリックすると名前と規模が出るが、`capacity` だけを配ると
    # **筑波大附属の 3 校が「150 人」を実数として名乗る**（在籍者数を
    # 公表していないので既定値を置いてある）。事業所側も同じで、
    # 定員欄が空だった 9,748 行（69.0%）は種別ごとの仮定員である。
    # どちらも `data/processed` には印が付いているのに、配信で落ちていた。
    #
    # **値の一致で後から復元してはいけない**（CLAUDE.md）——実定員が
    # たまたま仮定員と同じ 20 人だった行が 2,934 件あり、それを仮定側に
    # 数えると「需要寄与の 68.9% が仮定員」（実際は 25.1%）になる。
    # 正規化の時点で立てた列をそのまま運ぶ。
    #
    # True のときだけ載せる。False を全行に書くと、印の付かない
    # 駅・クリニックと区別が付かないうえ、配信量も無駄に増える。
    assumed = pd.Series(pd.NA, index=demand_points.index, dtype="object")
    for col in ("capacity_assumed", "students_assumed"):
        if col in demand_points.columns:
            assumed = assumed.mask(demand_points[col].fillna(False).astype(bool), True)
    demand_points = demand_points.assign(assumed=assumed)

    _write_geojson(
        WEB_DATA / "demand_points.geojson",
        gpd.GeoDataFrame(demand_points, crs=layers["welfare"].crs),
        ["name", "kind", "capacity", "assumed", "layer", "source"],
        with_xy=True,
    )

    # --- 騒音の測定地点 ---
    #
    # **需要側の点と混ぜない。** 事業所も駅も「そこに在るもの」だが、
    # 測定点は**調査がそこを測ったという事実**であって、施設ではない。
    # 同じファイルに入れると `layer` 列の値が 1 つ増えるだけに見え、
    # 地図でも同じ色の点になる（画面では白抜きの点として描き分ける）。
    #
    # **なぜ配信するか。** この作品でいちばん大きい既知の偏りは
    # 「騒音は幹線道路の道路端しか測っていない」こと（`docs/issues.md` A1）で、
    # 静かなのではなく測っていない場所が 496 区画ある（A6）。
    # **その偏りは文章では伝わらない**——測定点が道路に沿って並び、
    # 住宅地の内側が空白であることは、地図に出せば一目で分かる。
    #
    # `laeq_db` は 5 年分（令和元〜5年度）の同一地点平均。年度間の標準偏差は
    # 中央値 0.55dB で、測定の丸め（1dB 単位）と同じ桁である。
    #
    # 利用条件は確認済み（`docs/issues.md` C5）。2026-08-05 に環境局
    # 自動車環境課へ電話で照会し、**測定地点と騒音レベルの二次利用も可**との
    # 回答を得ている。**CC BY 4.0 とは名乗らない**——口頭の許諾であって
    # 名前の付いたライセンスではないので、`SOURCES` の `license` には
    # 回答の内容をそのまま書いてある。
    noise_points = layers["noise"].assign(layer="noise")
    _write_geojson(
        WEB_DATA / "noise_points.geojson",
        noise_points,
        # `survey` は落とさない。**測定点の選ばれ方が違う 2 つの調査を
        # 混ぜている**ので、どちらの点かは画面の吹き出しまで運ぶ
        # （`docs/issues.md` A1）。混ぜたことが見えなくなるのが一番まずい。
        ["name", "laeq_db", "years", "n_years", "survey", "layer", "source"],
        with_xy=True,
    )

    # --- 公園（緑・公園被覆を作っているもの） ---
    #
    # **施設ではない。** 需要側の点（事業所・学校・駅・クリニック）とも、
    # 供給側のホストとも別に配る。ここに在るのは「この区画の被覆率を
    # 作った公園」で、**退避先として評価したものではない**
    #（屋外が退避先になるかは当事者に確かめていない）。
    #
    # **点として配る。** 元データ（P13）が点で、形は入っていない——
    # 被覆率は面積の等しい円に置き換えて出している。円の半径は
    # `r = √(A/π)` で復元できるので、面積だけ運べば足りる。
    # **円であることは画面が言う**（吹き出しに「半径 18m 相当の円」と出す）。
    parks_out = layers["parks"].copy()
    parks_out["r_m"] = np.round(
        np.sqrt(parks_out["area_m2"].to_numpy() / math.pi), 1
    )
    # 円の中心＝元の点。面積から作った円なので centroid で元に戻る。
    parks_out = parks_out.set_geometry(parks_out.geometry.centroid)
    _write_geojson(
        WEB_DATA / "parks.geojson",
        parks_out,
        ["name", "area_m2", "r_m", "source"],
        with_xy=True,
    )

    _write_json(WEB_DATA / "cards.json", cards)
    _write_json(WEB_DATA / "proposals.json", proposals)

    # --- meta.json ---
    meta = {
        "generated_at": generated_at,
        "data_mode": "live" if live else "fixture",
        "layer_provenance": provenance,
        "real_layer_count": sum(1 for v in provenance.values() if v == "real"),
        "layer_total": len(provenance),
        # 1 レイヤーでも模擬が残っていれば、数値を引用してはいけない。
        "synthetic": any(v == "synthetic" for v in provenance.values()),
        "synthetic_notice": _synthetic_notice(provenance),
        "target_wards": TARGET_WARDS,
        "bbox": [round(float(v), 6) for v in mesh_gdf.to_crs(CRS_GEOGRAPHIC).total_bounds],
        "mesh_level": level,
        "mesh_label": meshlib.LEVEL_LABEL[level],
        "mesh_count": len(scored),
        # 「既存施設では到達不可」。**この作品でいちばん頑健な出力**なので
        # 画面の見出しに使う。重みもスコアも帯域も通っておらず、
        # 最寄りの公共施設までの距離だけで決まる（docs/issues.md A4 の対処方針）。
        #
        # mid_or_above は区内の優先度の中央値で切った件数で、既定重みでの値。
        # **画面で計算し直さない**——しきい値の取り方（区ごと・母数は区内の
        # 全メッシュ）を TypeScript にもう 1 つ書くと、静かに食い違う。
        "unreachable": _unreachable_summary(scored),
        # **この作品で唯一の外部照合。** 内部整合（parity・selftest・
        # doc_numbers）をどれだけ積んでも、モデルが現実を当てている証拠には
        # ならない。既に置かれているものと突き合わせて初めて外から確かめたことになる。
        "calm_spaces": _calm_space_report(scored),
        # 各層が特定の区とどれだけ紐づいているか（docs/issues.md A1）。
        #
        # **配信する理由は検査のため。** 騒音の「世田谷ダミーとの相関」は
        # 5 回引用してきた数値なのに、**出す経路がコードの側に無く**、
        # 文書に貼った再現スクリプトだけが頼りだった。しかもその手順は
        # 区の割り当てが本体と違い、文書の値を再現できなかった
        #（score.ward_dependence_report のコメント）。
        # ここへ載せると `tools/doc_numbers.py` が status.md と突き合わせる。
        "ward_dependence": _ward_dependence_summary(scored),
        "priority_alpha": PRIORITY_ALPHA,
        "priority_beta": PRIORITY_BETA,
        "host_max_distance_m": HOST_MAX_DISTANCE_M,
        # 「この区画の実数」の各行が、どの半径で数えた値なのか。
        #
        # **画面はこれを節見出しに出す。** かつては 8 行を平らに並べていて、
        # 半径 800m の件数と 700m の件数と区画自身の値が同じ見た目だった。
        # ここを TypeScript に直接書くと、帯域を動かしたときに
        # ラベルだけが古い半径を主張し続ける（`_attach_facts` と同じ
        # 定数から出しているので、そのズレが起こらない）。
        "fact_radius_m": {
            "welfare": BANDWIDTH_M["welfare_capacity"],
            "school": BANDWIDTH_M["sped_school"],
            "clinic": BANDWIDTH_M["clinic"],
            # 駅も他の需要 3 層と同じ規則——その層の帯域を出す。
            "station": BANDWIDTH_M["station_flow"],
            "host": HOST_MAX_DISTANCE_M,
            # 最寄り 1 駅（＝区画の呼び名）を探す上限。件数の半径ではない。
            "station_max": STATION_MAX_DISTANCE_M,
            # 騒音の内挿の打ち切り距離。**帯域でも徒歩圏でもない**——
            # この距離の内に測定点が 1 つも無ければ、その区画の騒音は
            # 測定ではなく 23 区の中央値である。
            "noise": NOISE_IDW_MAX_DISTANCE_M,
        },
        # **10 層のうち、スコアに入るのは 8 層。** 画面が「実データ 10/10」と
        # 「評価に使う 8 つのレイヤー」を別々に出していて、対応がどこにも
        # 書かれていなかった（外部からの指摘で発覚）。
        #
        # **10 と 8 の食い違いは、書き換えて消すものではない。**
        # スコアに入らない 2 層のうち hosts は「到達不可」という
        # この作品でいちばん頑健な出力を作っている層で、
        # スコアに入らないことがそのまま長所である。
        "layer_roles": _layer_roles(provenance),
        # 提言リスト（＝順位表）に出す件数。TypeScript 側に重複定義を作らない
        #（画面と配信 JSON が別物になっていた。config.RANKING_OPTIONS 参照）。
        # **選べるようにしてあるのは、打ち切りに根拠が無いことを隠さないため。**
        "ranking_options": list(RANKING_OPTIONS),
        "ranking_default_n": RANKING_DEFAULT_N,
        "presets": [
            {
                "id": p["id"],
                "label": p["label"],
                "note": p["note"],
                "weights": p["weights"],
            }
            for p in PRESETS
        ],
        "components": [
            {
                "key": c.key,
                "label": c.label,
                # どのデータ層から作られているか。画面が
                # 「10 層のうちこの 8 層」を対応付けて出すために配信する。
                "layer": c.layer,
                "side": c.side,
                "weight": c.weight,
                "sign": c.sign,
                "zeroIsAbsence": c.zero_is_absence,
                "source": c.source,
                "rationale": c.rationale,
                # **順位の母数。** 「地域内順位」と書くだけでは足りない——
                # 値 0 は「存在しない」として厳密に 0 に固定し、**正の値を持つ
                # 区画の中だけで順位を付けている**ので、母数は層ごとに違う。
                # 特別支援学校は 5,881 区画が 0 なので母数 3,626 で、
                # 「0.5」は 9,507 区画の真ん中ではなく「学校の徒歩圏に入っている
                # 3,626 区画の真ん中」を意味する。画面がここを出していなかった。
                # 絶対尺度の層は順位を使わないので None。
                "rank_denominator": (
                    None
                    if c.absolute is not None
                    else int(
                        (pd.to_numeric(scored[c.key], errors="coerce").fillna(0.0) > 0).sum()
                    )
                    if c.zero_is_absence
                    else len(scored)
                ),
                # 絶対尺度の層は「対象地域内の相対順位」という但し書きが要らない。
                # 画面でそこを区別して見せるために配信する（docs/issues.md B2）。
                "absolute": (
                    None
                    if c.absolute is None
                    else {
                        "label": c.absolute.label,
                        "lo": c.absolute.lo,
                        "hi": c.absolute.hi,
                        "basis": c.absolute.basis,
                    }
                ),
            }
            for c in ALL_COMPONENTS
        ],
        # **実際に使った出典だけを配信する。** レジストリには検討しただけの
        # 出典も入っており（ODPT・不動産情報ライブラリ・鉄道騒音・手帳交付状況・
        # 既存スペースの 5 件）、全部出すと「16 出典を使っている」に見える。
        # 使っていないものは件数だけ別に伝える。
        "sources": [
            {
                "key": s.key,
                "label": s.label,
                "url": s.url,
                "license": s.license,
                "note": s.note,
                "layer": s.layer,
                "vintage": s.vintage,
                "count": _source_row_count(layers, s),
            }
            for s in SOURCES.values()
            if s.layer is not None
        ],
        "unused_source_count": sum(1 for s in SOURCES.values() if s.layer is None),
        "layer_counts": {
            k: int(len(v))
            for k, v in layers.items()
            if hasattr(v, "__len__") and not k.startswith("_")
        },
    }
    _write_json(WEB_DATA / "meta.json", meta)


def _source_row_count(layers: dict, source) -> int | None:
    """その出典が実際に書いた行数。数えられなければレイヤー全体の件数。

    **レイヤーの件数をそのまま出すと、複数の出典が書く層で嘘になる。**
    騒音は令和元〜5年度（都）と令和6年度（環境GIS＋）の 2 出典で、
    出典一覧が**どちらにも 1,058 件**と出ていた——令和6年度が 1,058 点
    あるように読める（実際は 167 点）。ホスト施設でも同じことが起きていた。

    **0 件になったら数えない。** 学校の規模（都教委の在籍者数）のように
    **行ではなく属性を書く出典**があり、`source` 列にはそちらの名前が
    入っている。P29 は行を書いているのに `source` 列には出てこないので、
    厳密に数えると 0 件になる。**「0 件の出典」は使っていない出典に見える**
    ので、そのときはレイヤー全体の件数へ戻す。
    """
    layer = layers.get(source.layer)
    if layer is None or not hasattr(layer, "__len__"):
        return None
    if "source" in getattr(layer, "columns", []):
        # 1 行が複数の出典を持つことがある（同じ地点を 2 つの調査が測った）。
        stored = layer["source"].fillna("").astype(str)
        hit = stored.map(
            lambda v: source.label in _current_source_label(v).split(SOURCE_JOIN)
        )
        if int(hit.sum()):
            return int(hit.sum())
    return int(len(layer))


def _layer_roles(provenance: dict[str, str]) -> list[dict]:
    """データの層 10 個を「スコアに入る 8 層」と「入らない 2 層」に分けて出す。

    **分類漏れで止まる。** 新しい層を足して `Component.layer` にも
    `SUPPORT_LAYERS` にも書かなかったとき、画面の「10 層のうち 8 層が
    スコアに入る」だけが黙って古くなる——数が合わないことは
    出力を眺めても分からない（8 と 10 の食い違いは、外部から
    指摘されるまで気付けなかった。それを二度やらないための検査）。

    並び順は `ALL_COMPONENTS` の定義順（＝画面の並び順）で、
    スコアに入らない層をその後ろに置く。
    """
    by_layer = {c.layer: c for c in ALL_COMPONENTS}
    unknown = sorted(set(provenance) - set(by_layer) - set(SUPPORT_LAYERS))
    if unknown:
        raise ValueError(
            f"どちらにも分類されていないデータ層がある: {'・'.join(unknown)}。"
            "スコアに入るなら Component.layer に、入らないなら "
            "config.SUPPORT_LAYERS に書くこと。"
            "**書かないと画面の「10 層のうち 8 層」だけが古くなる。**"
        )
    missing = sorted((set(by_layer) | set(SUPPORT_LAYERS)) - set(provenance))
    if missing:
        raise ValueError(
            f"存在しないデータ層を分類している: {'・'.join(missing)}。"
            "レイヤー名が変わったか、層が消えている。"
        )

    roles = [
        {
            "layer": c.layer,
            "label": c.label,
            "role": "score",
            "component": c.key,
            "side": c.side,
            "note": "",
            "provenance": provenance[c.layer],
        }
        for c in ALL_COMPONENTS
    ]
    roles += [
        {
            "layer": key,
            "label": label,
            "role": "support",
            "component": None,
            "side": None,
            "note": note,
            "provenance": provenance[key],
        }
        for key, (label, note) in SUPPORT_LAYERS.items()
    ]
    return roles


def _synthetic_notice(provenance: dict[str, str]) -> str | None:
    """模擬データが残っている場合の警告文。UI のバナーに出る。"""
    fake = sorted(k for k, v in provenance.items() if v == "synthetic")
    if not fake:
        return None
    return (
        f"{len(fake)}/{len(provenance)} レイヤーが模擬データ（{'・'.join(fake)}）。"
        "これらに由来する数値・施設名は架空であり、実際の提言として引用できない。"
    )


def _round_geometry(geom: dict, ndigits: int = 6) -> dict:
    """座標を丸めて配信サイズを削る。小数6桁は約 10cm 相当で、
    250m メッシュの描画には十分すぎる精度。"""

    def walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                return [round(float(v), ndigits) for v in c]
            return [walk(x) for x in c]
        return c

    return {**geom, "coordinates": walk(geom["coordinates"])}


def _write_json(path, obj) -> None:
    path.write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"[write] {path.relative_to(path.parents[3])} ({path.stat().st_size:,} B)")


# **判定は config.py に置いてある。** ここに実装を持っていた頃、
# `fetch.py` の `--append` 側は現在の label しか見ておらず、
# **改名した出典を再正規化しても出力が変わらない**状態になっていた
# （2026-08-07 に発覚）。同じ判定が 2 箇所に要るなら片方だけ古くなる。
_current_source_label = current_source_label


def _write_geojson(
    path, gdf: gpd.GeoDataFrame, cols: list[str], with_xy: bool = False
) -> None:
    """点・面を GeoJSON で書き出す。

    with_xy=True なら平面直角座標系（EPSG:6677）の x / y も props に載せる。

    **これは装飾ではない。** 画面が「徒歩圏の事業所 60 件」を地図上で
    光らせるとき、**表示される点の数と表の数字が一致しなければ意味がない**。
    ブラウザで緯度経度から距離を出すと、投影の違いで 800m の境界付近が
    1〜2 件ズレる。Python が数えているのと同じ平面座標を配ってしまえば、
    両者は同じ引き算をすることになり、ズレる余地が無くなる
    （`tools/facility_parity.mjs` が全 9,507 区画で一致を検査する）。
    """
    cols = [c for c in cols if c in gdf.columns]
    xy = gdf.to_crs(CRS_PROJECTED).geometry if with_xy else None
    features = []
    for i, (_, row) in enumerate(gdf.iterrows()):
        props = {}
        for c in cols:
            v = row[c]
            if pd.isna(v):
                continue
            # 出典名は config が唯一の出所。processed に焼き付いている
            # 旧 label を現在の label へ寄せる（Source.aliases）。
            if c == "source":
                v = _current_source_label(str(v))
            props[c] = v.item() if hasattr(v, "item") else v
        if xy is not None:
            g = xy.iloc[i]
            # cm 精度。距離の誤差は 1cm 未満で、半径の境界に 1cm 以内で
            # 載る点はこのデータには 1 件も無い（facility_parity が検査する）。
            props["x"] = round(float(g.x), PUBLISH_XY_DECIMALS)
            props["y"] = round(float(g.y), PUBLISH_XY_DECIMALS)
        features.append(
            {
                "type": "Feature",
                "geometry": _round_geometry(row.geometry.__geo_interface__),
                "properties": props,
            }
        )
    _write_json(path, {"type": "FeatureCollection", "features": features})


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="calmgap-tokyo データパイプライン")
    ap.add_argument(
        "--live",
        action="store_true",
        help="実オープンデータを使う（data/processed を読む）。既定は模擬データ。",
    )
    ap.add_argument(
        "--level", type=int, default=MESH_LEVEL, choices=[3, 4, 5], help="メッシュ次数"
    )
    ap.add_argument("--top", type=int, default=TOP_N_CARDS, help="根拠カードの件数")
    ap.add_argument("--report", action="store_true", help="点検表を表示する")
    ap.add_argument(
        "--sensitivity",
        action="store_true",
        help="感度分析を実行し sensitivity.json を書き出す",
    )
    args = ap.parse_args(argv)

    if not args.live:
        print("=" * 70)
        print("  模擬データモード — 出力はすべて架空。実際の提言に使用不可。")
        print("  実データで動かすには: python -m etl.fetch && python -m etl.build --live")
        print("=" * 70)

    layers, provenance = load_layers(args.live)
    mesh_gdf = build_mesh_table(layers, args.level)

    normalized = score.normalize_components(mesh_gdf.drop(columns="geometry"))

    # 配信精度へ丸めてから合成する。
    # ブラウザへ渡すのは丸めた n_* であり、そこから再計算した結果が
    # ここで書き出す demand/load/priority と一致していなければならない。
    # 丸め方も JS と揃える（score.publish_round のコメント参照）。
    for comp in ALL_COMPONENTS:
        col = f"n_{comp.key}"
        normalized[col] = score.publish_round(normalized[col])

    scored = score.compose(normalized)

    host_df = hostlib.assign_hosts(mesh_gdf, layers["hosts"])
    scored = scored.merge(host_df, on="mesh_code", how="left")

    cards = hostlib.build_cards(scored, args.top)
    # 提言リスト（＝優先度の順位表）。**単位は区画。**
    # 根拠カードと同じ `build_cards` から作るので、2 つが別物になり得ない
    #（かつて proposals.json と画面が別の母数で束ねていた事故の再発防止）。
    # cards.json は上位 20 件に要因の内訳が付いたもので、こちらの部分集合。
    proposals = hostlib.build_ranking(
        hostlib.build_cards(scored, RANKING_DEFAULT_N), RANKING_DEFAULT_N
    )

    # meta.json と sensitivity.json が同じ値を持つことで、画面が
    # 「別のビルドで作られた感度分析」を無言で出すのを防ぐ（docs/issues.md F-4）。
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    write_outputs(
        mesh_gdf,
        scored,
        layers,
        cards,
        proposals,
        args.live,
        args.level,
        provenance,
        generated_at,
    )

    print(f"\n[提言] 上位 {len(proposals)} 区画（上位 5 件）:")
    for p in proposals[:5]:
        where = " ".join(x for x in (p["ward"], p["station"]) if x)
        reach = "徒歩圏に公共施設なし" if p["unreachable"] else f"公共施設{p['host_count']}件"
        print(
            f"  {p['rank']:>2}位 {p['mesh_code']} {where}"
            f"  優先度 {p['priority']:.3f}  {reach}"
            f"  隣接 {p['adjacent_n']}"
        )

    if args.sensitivity:
        # 重み以外の固定値まで揺さぶるには、正規化済みの列だけでは足りない
        #（集計まで遡って計算し直すため、メッシュ形状と入力レイヤーが要る）。
        model = sensitivity.FixedValueModel(mesh_gdf, layers)
        result = sensitivity.run(normalized, model)
        result["host_distance"] = sensitivity.host_distance_report(
            scored, mesh_gdf, layers["hosts"]
        )
        # 画面が古い感度分析を無言で出さないための照合キー（docs/issues.md F-4）。
        result["generated_at"] = generated_at
        _write_json(WEB_DATA / "sensitivity.json", result)
        print(sensitivity.format_report(result))

    if args.report:
        print("\n[点検] 優先度 上位10メッシュ")
        print(score.sanity_report(scored).to_string(index=False))
        print("\n[点検] 構成要素間の相関（0.9超は二重計上を疑う）")
        print(score.correlation_report(scored).to_string())

        # 2 変数間の相関では「4 層が揃って 1 つの現象を指している」形の
        # 重複が見えない（docs/issues.md A2）。VIF と主成分で見る。
        vif_df, pc_df = score.collinearity_report(scored)
        print("\n[点検] VIF（他の層から予測できてしまう度合い。5超は多重共線を疑う）")
        print(vif_df.to_string(index=False))
        print("\n[点検] 主成分（同符号で並ぶ層が、同じ現象を数え直している層）")
        print(pc_df.to_string())

        # 「この層は実質○○区ダミーではないか」（docs/issues.md A1）。
        # **文書が 5 回引用してきた指標なのに、出す経路がここに無かった。**
        # 区は決め打ちしない——決め打つと、偏りが別の区へ移ったときに
        # 気付けない（score.ward_dependence_report のコメント）。
        wd = score.ward_dependence_report(scored)
        if not wd.empty:
            print("\n[点検] 区ダミーとの相関（層が 1 つの区に張り付いていないか）")
            print(wd.drop(columns=["key", "ward"]).to_string(index=False))
            print("       0 から離れているほど、その層はその区のダミーとして働いている")
            # 上の表は区を決め打たない。こちらは決め打つ——A1 の系列を
            # 途中で見る区を変えずに続けるため（config.A1_TRACKED_PAIR）。
            tracked = score.ward_dummy_correlation(scored, *A1_TRACKED_PAIR)
            if tracked is not None:
                key, ward = A1_TRACKED_PAIR
                label = next(
                    (c.label for c in ALL_COMPONENTS if c.key == key), key
                )
                print(
                    f"       issues.md A1 が並べてきた組: {label} × {ward} "
                    f"{tracked:+.3f}（系列の比較用に区を固定している）"
                )
            spread = score.ward_value_spread(scored, "f_noise_db")
            if len(spread) >= 2:
                lo, hi = spread.iloc[0], spread.iloc[-1]
                print(
                    f"       騒音の内挿平均は {lo['区']} {lo['f_noise_db']:.1f}dB 〜 "
                    f"{hi['区']} {hi['f_noise_db']:.1f}dB（"
                    f"{hi['f_noise_db'] - lo['f_noise_db']:.1f}dB の開き）"
                )

        # 到達不可は生の件数を区の間で並べても意味を持たない
        #（非市街地の面積比でほぼ決まる）。hosts.reach_report のコメント参照。
        reach = hostlib.reach_report(scored)
        total_unreachable = int(reach["到達不可"].sum())
        total_mid = int(reach["中位以上"].sum())
        if total_unreachable:
            print(
                f"\n[点検] 到達不可区画 {total_unreachable:,}件 / "
                f"うち区内で優先度が中位以上 {total_mid:,}件 "
                f"({total_mid / total_unreachable * 100:.0f}%)"
            )
            print("       区をまたいで並べるなら「中位以上」の側を使う")
        else:
            print("\n[点検] 到達不可区画は 0 件")
        print(reach.to_string(index=False))

        # ホスト件数を「施設の数」として読めないことを数で出す
        #（hosts.proximity_report。文書側の値が再現できなかった経緯もそこに）。
        prox = hostlib.proximity_report(layers["hosts"])
        print(
            f"\n[点検] ホスト {prox['ホスト']:,}件 のうち "
            f"{prox['半径m']:.0f}m 以内に別の行が並ぶ組 {prox['組']:,}"
            f"（同一種別 {prox['同一種別']:,} / 別種別 {prox['別種別']:,}）"
        )
        print("       同じ建物を別種別で 2 度数えている疑いがある。件数 ≠ 施設数")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
