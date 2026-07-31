"""
パイプライン全体のオーケストレータ。

    python -m etl.build              # 模擬データで実行（既定・ネットワーク不要）
    python -m etl.build --live       # 実オープンデータで実行
    python -m etl.build --level 4    # 4次メッシュ(500m)で実行
    python -m etl.build --report     # 上位メッシュと相関の点検表を表示

出力は web/public/data/ 配下:
    mesh.geojson       メッシュ形状 + 正規化済み構成要素 + 既定重みでのスコア
    cards.json         上位メッシュの根拠カード
    proposals.json     隣接する上位区画を地区にまとめた提言リスト
                       （施設単位ではない。理由は etl/hosts.py の build_proposals）
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
    CRS_GEOGRAPHIC,
    SOURCES,
    STUDY_BBOX,
    TARGET_WARDS,
    TOP_N_CARDS,
    WEB_DATA,
)


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

    mesh_gdf["f_welfare_n"] = aggregate.count_within(
        mesh_gdf, layers["welfare"], walk
    ).to_numpy()
    mesh_gdf["f_welfare_cap"] = aggregate.count_within(
        mesh_gdf, layers["welfare"], walk, "capacity"
    ).to_numpy()
    mesh_gdf["f_school_n"] = aggregate.count_within(
        mesh_gdf, layers["schools"], BANDWIDTH_M["sped_school"]
    ).to_numpy()
    mesh_gdf["f_clinic_n"] = aggregate.count_within(
        mesh_gdf, layers["clinics"], walk
    ).to_numpy()

    # 供給側の実数。**これが供給側について言える唯一のこと**——徒歩圏に
    # 屋内の公共空間が幾つ在るか。どれが適するかは測っていない。
    # 数えているのは施設一覧の行数であって建物の数ではない（100m 以内に
    # 別種別の行が並ぶ組が残っている。docs/issues.md）。
    mesh_gdf["f_host_n"] = aggregate.count_within(
        mesh_gdf, layers["hosts"], HOST_MAX_DISTANCE_M
    ).to_numpy()

    station = aggregate.nearest_feature(
        mesh_gdf, layers["stations"], ["name", "capacity"], max_distance_m=1500.0
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


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def _feature_properties(row: pd.Series) -> dict:
    """配信サイズを抑えるため、必要な列だけを丸めて出す。"""
    props = {"c": row["mesh_code"]}
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
        ("f_school_n", int),
        ("f_clinic_n", int),
        ("f_station_riders", int),
        ("f_station_dist", int),
        ("f_host_n", int),
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


def write_outputs(
    mesh_gdf: gpd.GeoDataFrame,
    scored: pd.DataFrame,
    layers: dict,
    cards: list[dict],
    proposals: list[dict],
    live: bool,
    level: int,
    provenance: dict[str, str],
) -> None:
    WEB_DATA.mkdir(parents=True, exist_ok=True)

    # --- mesh.geojson ---
    features = [
        {
            "type": "Feature",
            "geometry": _round_geometry(geom),
            "properties": _feature_properties(row),
        }
        for geom, (_, row) in zip(
            mesh_gdf.geometry.map(lambda g: g.__geo_interface__), scored.iterrows()
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
    _write_geojson(
        WEB_DATA / "demand_points.geojson",
        gpd.GeoDataFrame(demand_points, crs=layers["welfare"].crs),
        ["name", "kind", "capacity", "layer", "source"],
    )

    _write_json(WEB_DATA / "cards.json", cards)
    _write_json(WEB_DATA / "proposals.json", proposals)

    # --- meta.json ---
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        "priority_alpha": PRIORITY_ALPHA,
        "priority_beta": PRIORITY_BETA,
        "host_max_distance_m": HOST_MAX_DISTANCE_M,
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
        "sources": [
            {
                "key": s.key,
                "label": s.label,
                "url": s.url,
                "license": s.license,
                "note": s.note,
            }
            for s in SOURCES.values()
        ],
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


def _write_geojson(path, gdf: gpd.GeoDataFrame, cols: list[str]) -> None:
    cols = [c for c in cols if c in gdf.columns]
    features = []
    for _, row in gdf.iterrows():
        props = {}
        for c in cols:
            v = row[c]
            if pd.isna(v):
                continue
            props[c] = v.item() if hasattr(v, "item") else v
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
    proposals = hostlib.build_proposals(cards)

    write_outputs(
        mesh_gdf, scored, layers, cards, proposals, args.live, args.level, provenance
    )

    print(f"\n[提言] 隣接区画を地区にまとめた候補 {len(proposals)} 件（上位 3 件）:")
    for p in proposals[:3]:
        print(
            f"  {p['best_rank']:>2}位 {p['area_label']}"
            f"（{p['mesh_count']}区画 / 徒歩圏に施設が無い区画 {p['unreachable_meshes']}）"
        )

    if args.sensitivity:
        result = sensitivity.run(normalized)
        _write_json(WEB_DATA / "sensitivity.json", result)
        print(sensitivity.format_report(result))

    if args.report:
        print("\n[点検] 優先度 上位10メッシュ")
        print(score.sanity_report(scored).to_string(index=False))
        print("\n[点検] 構成要素間の相関（0.9超は二重計上を疑う）")
        print(score.correlation_report(scored).to_string())

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
