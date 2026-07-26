"""
上位メッシュへの「ホスト施設」割当と、根拠カードの生成。

本プロジェクトは分析で終わらせず「この施設に置け」まで言い切る。
ヒートマップは行政の意思決定にそのまま使えないが、
施設名の入った提言リストは予算会議の資料になる。
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from .config import (
    ALL_COMPONENTS,
    TARGET_WARDS,
    DEMAND_COMPONENTS,
    HOST_MAX_DISTANCE_M,
    HOST_PREFERENCE,
    LOAD_COMPONENTS,
    CRS_PROJECTED,
)

_COMPONENT_BY_KEY = {c.key: c for c in ALL_COMPONENTS}


# ---------------------------------------------------------------------------
# ホスト施設の割当
# ---------------------------------------------------------------------------


def assign_hosts(
    mesh_gdf: gpd.GeoDataFrame,
    hosts: gpd.GeoDataFrame,
    max_distance_m: float = HOST_MAX_DISTANCE_M,
) -> pd.DataFrame:
    """各メッシュに最適なホスト施設を 1 件割り当てる。

    最寄りを機械的に選ぶのではなく、`HOST_PREFERENCE` の順位を優先する。
    150m 先の駅より 400m 先の図書館を選ぶ、という判断を入れている。
    図書館は静穏であることが既に運営方針に含まれており、
    一室を転用する合意形成の難易度が最も低いため。

    max_distance_m 以内に候補が無いメッシュは host_name が空になる。
    これは失敗ではなく「既存の公共施設では届かない＝新設が必要」という
    それ自体が提言になる出力。
    """
    idx = pd.Index(mesh_gdf["mesh_code"], name="mesh_code")
    empty = pd.DataFrame(
        {
            "mesh_code": idx,
            "host_name": "",
            "host_kind": "",
            "host_ward": "",
            "host_distance_m": np.nan,
            "host_lon": np.nan,
            "host_lat": np.nan,
        }
    ).reset_index(drop=True)

    if hosts is None or len(hosts) == 0:
        return empty

    m = mesh_gdf.to_crs(CRS_PROJECTED)
    h = hosts.to_crs(CRS_PROJECTED)

    mesh_xy = np.column_stack(
        [m.geometry.centroid.x.to_numpy(), m.geometry.centroid.y.to_numpy()]
    )
    host_xy = np.column_stack([h.geometry.x.to_numpy(), h.geometry.y.to_numpy()])

    # 種別の優先順位（小さいほど良い）。未知種別は最下位扱い。
    worst = max(HOST_PREFERENCE.values()) + 1
    pref = np.array(
        [HOST_PREFERENCE.get(k, worst) for k in h["host_kind"].fillna("")], dtype=float
    )

    dist = np.sqrt(
        (mesh_xy[:, 0][:, None] - host_xy[None, :, 0]) ** 2
        + (mesh_xy[:, 1][:, None] - host_xy[None, :, 1]) ** 2
    )

    # 距離を優先順位より下位のキーにするため、
    # 「優先順位 * 十分大きな値 + 距離」の合成コストで最小を採る。
    cost = pref[None, :] * (max_distance_m * 10.0) + dist
    cost = np.where(dist <= max_distance_m, cost, np.inf)

    best = np.argmin(cost, axis=1)
    reachable = np.isfinite(cost[np.arange(len(cost)), best])

    names = h["name"].to_numpy()
    kinds = h["host_kind"].fillna("").to_numpy()
    # 区界をまたいだ割当は消さない。すぐ隣の区の施設が実際に最寄りである
    # ことは珍しくなく、当事者にとって区境は意味を持たない。
    # ただし提言先の自治体が変わるので、区名を持ち回って UI で明示する。
    wards = (
        h["ward"].fillna("").to_numpy()
        if "ward" in h.columns
        else np.full(len(h), "")
    )
    hlon = hosts.geometry.x.to_numpy()
    hlat = hosts.geometry.y.to_numpy()

    return pd.DataFrame(
        {
            "mesh_code": idx,
            "host_name": np.where(reachable, names[best], ""),
            "host_kind": np.where(reachable, kinds[best], ""),
            "host_ward": np.where(reachable, wards[best], ""),
            "host_distance_m": np.where(
                reachable, dist[np.arange(len(dist)), best].round(0), np.nan
            ),
            "host_lon": np.where(reachable, hlon[best], np.nan),
            "host_lat": np.where(reachable, hlat[best], np.nan),
        }
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 根拠カード
# ---------------------------------------------------------------------------


def _top_factors(row: pd.Series, side: str, k: int = 3) -> list[dict]:
    """そのメッシュのスコアを押し上げている要因を寄与度順に返す。"""
    comps = DEMAND_COMPONENTS if side == "demand" else LOAD_COMPONENTS
    items = []
    for c in comps:
        n = float(row.get(f"n_{c.key}", 0.0))
        # 寄与度 = 正規化値 × 既定重み。減点レイヤーは符号を反映する。
        items.append(
            {
                "key": c.key,
                "label": c.label,
                "normalized": round(n, 3),
                "contribution": round(n * c.weight * c.sign, 3),
                "raw": round(float(row.get(c.key, 0.0)), 2),
                "source": c.source,
                "rationale": c.rationale,
            }
        )
    items.sort(key=lambda d: abs(d["contribution"]), reverse=True)
    return items[:k]


def _narrative(row: pd.Series, rank: int) -> str:
    """予算会議にそのまま出せる日本語の根拠文を組み立てる。"""
    d_factors = _top_factors(row, "demand", 2)
    l_factors = _top_factors(row, "load", 2)

    def pct(x: float) -> str:
        return f"上位{max(1, round((1 - x) * 100)):d}%"

    parts = [
        f"優先度 第{rank}位（対象地域内 {pct(float(row['priority']))}）。",
        f"需要スコア {float(row['demand']):.2f} / 負荷スコア {float(row['load']):.2f}。",
    ]

    if d_factors:
        names = "・".join(f["label"].split("（")[0] for f in d_factors)
        parts.append(f"需要側は{names}が押し上げている。")
    if l_factors:
        pos = [f for f in l_factors if f["contribution"] > 0]
        if pos:
            names = "・".join(f["label"].split("（")[0] for f in pos)
            parts.append(f"負荷側は{names}が支配的。")

    green = float(row.get("n_green", 0.0))
    if green < 0.15:
        parts.append("緑・公園被覆がほぼ無く、屋外に代替の退避先が存在しない。")

    host = str(row.get("host_name") or "")
    if host:
        dist = row.get("host_distance_m")
        where = f"（メッシュ重心から約{int(dist)}m）" if pd.notna(dist) else ""
        parts.append(f"設置候補: {host}{where}。")
        ward = str(row.get("host_ward") or "")
        if ward and ward not in TARGET_WARDS:
            parts.append(
                f"ただし{ward}の施設であり、対象区の所管外。"
                "区境をまたぐ連携が前提になる。"
            )
    else:
        parts.append(
            f"半径{int(HOST_MAX_DISTANCE_M)}m 以内に転用可能な公共施設が無い。"
            "既存ストックでは到達できず、新規整備または民間施設との連携が要る。"
        )
    return "".join(parts)


def build_cards(df: pd.DataFrame, top_n: int = 20) -> list[dict]:
    """上位メッシュの根拠カードを生成する。"""
    top = df.nlargest(top_n, "priority").reset_index(drop=True)
    cards = []
    for i, row in top.iterrows():
        rank = i + 1
        cards.append(
            {
                "rank": rank,
                "mesh_code": row["mesh_code"],
                "lon": round(float(row["lon"]), 6),
                "lat": round(float(row["lat"]), 6),
                "ward": row.get("ward", ""),
                "priority": round(float(row["priority"]), 3),
                "demand": round(float(row["demand"]), 3),
                "load": round(float(row["load"]), 3),
                "host_name": row.get("host_name", ""),
                "host_kind": row.get("host_kind", ""),
                "host_ward": row.get("host_ward", ""),
                "host_distance_m": (
                    None
                    if pd.isna(row.get("host_distance_m"))
                    else int(row["host_distance_m"])
                ),
                "demand_factors": _top_factors(row, "demand", 3),
                "load_factors": _top_factors(row, "load", 3),
                "narrative": _narrative(row, rank),
            }
        )
    return cards


def build_proposals(cards: list[dict], limit: int = 10) -> list[dict]:
    """カードを施設単位に束ね、重複のない提言リストにする。

    ひとつの図書館が隣接する複数の高優先度メッシュをまとめて受け持つことは多い。
    メッシュを羅列すると同じ施設が何度も出てきて提言として読めないため、
    施設ごとに集約し「何メッシュ分の需要を受け持つか」を併記する。
    この件数がそのまま費用対効果の説明になる。
    """
    by_host: dict[str, dict] = {}
    unreachable: list[dict] = []

    for card in cards:
        host = card["host_name"]
        if not host:
            unreachable.append(card)
            continue
        entry = by_host.get(host)
        if entry is None:
            by_host[host] = {
                "host_name": host,
                "host_kind": card["host_kind"],
                "best_rank": card["rank"],
                "priority": card["priority"],
                "lon": card["lon"],
                "lat": card["lat"],
                "ward": card["ward"],
                "covered_meshes": 1,
                "mesh_codes": [card["mesh_code"]],
                "narrative": card["narrative"],
            }
        else:
            entry["covered_meshes"] += 1
            entry["mesh_codes"].append(card["mesh_code"])

    proposals = sorted(by_host.values(), key=lambda d: d["best_rank"])[:limit]
    for p in proposals:
        if p["covered_meshes"] > 1:
            p["narrative"] += (
                f"この施設 1 箇所で上位{p['covered_meshes']}メッシュ分の"
                "需要を受け持てる。"
            )

    return proposals + [
        {
            "host_name": "",
            "host_kind": "",
            "best_rank": c["rank"],
            "priority": c["priority"],
            "lon": c["lon"],
            "lat": c["lat"],
            "ward": c["ward"],
            "covered_meshes": 1,
            "mesh_codes": [c["mesh_code"]],
            "narrative": c["narrative"],
        }
        for c in unreachable[:3]
    ]
