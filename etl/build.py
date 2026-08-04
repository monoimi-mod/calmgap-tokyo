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
    meta.json          構成要素定義・出典・生成条件

重みの掛け合わせは意図的にブラウザ側へ残してある。
ここで出力するのは「正規化済みの素材」であり、
最終スコアはユーザーがスライダーで動かした結果として決まる。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from functools import lru_cache

import geopandas as gpd
import numpy as np
import pandas as pd

from . import aggregate, fixtures, hosts as hostlib, mesh as meshlib, score, sensitivity
from .schema import ZONING_NAME
from .config import (
    ALL_COMPONENTS,
    BANDWIDTH_M,
    DEMAND_COMPONENTS,
    HOST_MAX_DISTANCE_M,
    LOAD_COMPONENTS,
    MESH_LEVEL,
    PRIORITY_ALPHA,
    PRIORITY_BETA,
    PRESETS,
    RANKING_DEFAULT_N,
    RANKING_OPTIONS,
    CRS_GEOGRAPHIC,
    CRS_PROJECTED,
    PUBLISH_XY_DECIMALS,
    SOURCES,
    STUDY_BBOX,
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
    return mesh_gdf


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
    mesh_gdf["f_green_pct"] = np.round(mesh_gdf["green"].to_numpy() * 100, 1)
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
        ("f_crowding", int),
        ("f_noise_db", float),
        ("f_green_pct", float),
    ):
        v = row.get(key)
        if pd.notna(v) and float(v) != 0.0:
            props[key] = cast(v)
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
        },
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
                "count": int(len(layers[s.layer]))
                if s.layer in layers and hasattr(layers[s.layer], "__len__")
                else None,
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


@lru_cache(maxsize=1)
def _source_alias_map() -> dict[str, str]:
    """旧 label → 現在の label。SOURCES から組み立てる。"""
    out: dict[str, str] = {}
    for src in SOURCES.values():
        for old in src.aliases:
            out[old] = src.label
    return out


def _current_source_label(stored: str) -> str:
    """`data/processed` に焼き付いた出典名を、現在の label へ寄せる。

    **一致しないものはそのまま返す。** 学校の規模には
    「愛育学園 公表値（令和7年度4月1日現在）」「規模不明」のように
    レジストリに無い出典が正当に入っており、ここで止めると
    **出典を個別に書いたことそのものが罰になる**。
    """
    return _source_alias_map().get(stored, stored)


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
