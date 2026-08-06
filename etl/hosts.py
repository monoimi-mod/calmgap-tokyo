"""
既存の公共施設（供給側）との距離の集計と、根拠カード・提言リストの生成。

**この道具は区画について述べる。施設については述べない。**
需要も負荷もメッシュの属性から作っており、施設の適性を測る項目は
一つも無い。したがって出せる結論は「この区画の優先度が高い」までで、
「この建物に置け」ではない。供給側のレイヤーが答えるのは
「徒歩圏に屋内の公共空間がそもそも在るか」だけである。

かつては割当先の施設名を提言の見出しにしていた（`build_proposals` を参照）。
やめた経緯もそこに書いてある。
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from . import mesh as meshlib
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
    """各メッシュに、徒歩圏の公共施設を代表 1 件だけ割り当てる。

    **これは設置先の選定ではない。** この列の役割は二つに減っている。

    1. `max_distance_m` 以内に 1 件も無いメッシュを見つけること。
       これが「既存の公共施設では届かない」＝地図を眺めても出てこない出力で、
       失敗ではなくそれ自体が提言になる。
    2. その区画の徒歩圏に何が在るかを 1 件例示すること。

    どちらも「どこに置くべきか」の判断を含まない。`HOST_PREFERENCE` は
    例示に選ぶ順番を決めているだけで、到達可否には効かない（1 件でも
    圏内にあれば到達可能）。したがってこの順位を入れ替えてもスコアも
    到達不可の件数も動かず、カードに出る施設名だけが変わる。
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


def _target_ward(value) -> str:
    """対象区の一覧に載っている区名だけを返す。それ以外は空。"""
    name = str(value or "")
    return name if name in TARGET_WARDS else ""


def _narrative(row: pd.Series, rank: int, total: int) -> str:
    """予算会議にそのまま出せる日本語の根拠文を組み立てる。

    **「上位◯%」は順位から出す。優先度の値から出してはいけない。**
    かつては `(1 - priority) * 100` で計算していた。priority が
    パーセンタイル順位だった頃はそれで正しかったが、2026-08-03 に
    「需要 × 負荷」の生値へ変えたので成り立たなくなった
    （渋谷駅前は 792 位 = 上位 8% なのに、生値 0.7219 から出すと 28% になる）。
    **順位化をやめると、順位化を前提にした計算が黙って壊れる。**
    web/src/ui.ts の narrate() と対。片方を触ったら必ず両方。
    """
    d_factors = _top_factors(row, "demand", 2)
    l_factors = _top_factors(row, "load", 2)

    def pct(r: int) -> str:
        return f"上位{max(1, round(r / total * 100)):d}%"

    parts = [
        f"優先度 第{rank}位（対象地域内 {pct(rank)}）。",
        # 3 桁で出す。2 桁だと上位 100 件がすべて 0.99〜1.00 になり、
        # 実際には在る差が見えない（優先度は 0.99〜0.17 と動く）。
        f"需要 {float(row['demand']):.3f} × 負荷 {float(row['load']):.3f}"
        f" = {float(row['priority']):.3f}。",
    ]

    if d_factors:
        names = "・".join(f["label"].split("（")[0] for f in d_factors)
        parts.append(f"需要側は{names}が押し上げている。")
    if l_factors:
        pos = [f for f in l_factors if f["contribution"] > 0]
        if pos:
            names = "・".join(f["label"].split("（")[0] for f in pos)
            parts.append(f"負荷側は{names}が支配的。")

    # **かつてここは「屋外に代替の退避先が存在しない」と書いていた。誤りである。**
    #
    # この層が見ているのは**その 250m 区画に重なる公園の面積割合**だけで、
    # 隣の区画にある公園は数えていない。しかも元データ（P13）は点で、
    # 面積の等しい円に置き換えてある——**等価半径の中央値は 18m**
    #（3,835 件のうち 3,738 件が区画より小さい円）。つまり
    # 「被覆 0%」から言えるのは**その区画に公園が重なっていないこと**まで。
    # 30m 先に公園があっても 0% になる。
    #
    # **「退避先」という言葉も使わない。** 屋外が退避先になるかどうかは
    # 当事者に確かめていない（この層の rationale は負荷の低減であって、
    # 供給の話ではない。`config.py` の green を参照）。
    green = float(row.get("n_green", 0.0))
    if green < 0.15:
        parts.append(
            "緑・公園の被覆はほぼ無い。"
            "ただしこれは区画に重なる面積で、隣の区画にある公園は数えていない。"
        )

    # 供給側は「在るか / 幾つ在るか」までしか述べない。
    # かつてここに「設置候補: ◯◯図書館」と書いていたが、それはこのモデルが
    # 計算していない結論だった（施設の適性を測る項目が無い）。
    host = str(row.get("host_name") or "")
    if host:
        n = int(row.get("f_host_n") or 0)
        dist = row.get("host_distance_m")
        near = f"、最寄りは{host}で約{int(dist)}m" if pd.notna(dist) else f"（例: {host}）"
        parts.append(
            f"徒歩圏（{int(HOST_MAX_DISTANCE_M)}m）に区の公共施設が{n}件{near}"
            "（施設側の余剰空間も運営体制も測っておらず、適否の判断は含まない）。"
        )
    else:
        parts.append(
            f"半径{int(HOST_MAX_DISTANCE_M)}m 以内に区の公共施設が 1 件も無い。"
            "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要る。"
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
                # メッシュ自身の区名。地区の名前と提言先の自治体はこれで決まる。
                # 以前は施設側の区名で代用していたが、それは「この区画がどの区か」
                # ではなく「例示した施設がどの区か」で、区境際で食い違う。
                #
                # 対象区の一覧に無い名前は空にする。**ブラウザ側と揃えるため。**
                # 配信では区名を target_wards への添字で渡しており、一覧に無い
                # 名前は落ちる。ここで落とさないと、模擬データモードで
                # Python が「対象地域（模擬・矩形） 渋谷駅周辺」、
                # ブラウザが「渋谷駅周辺」と別の見出しを出す。
                "ward": _target_ward(row.get("ward")),
                "station": str(row.get("f_station_name") or ""),
                "host_count": int(row.get("f_host_n") or 0),
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
                "narrative": _narrative(row, rank, len(df)),
            }
        )
    return cards


def cluster_adjacent(codes: list[str]) -> list[list[int]]:
    """隣接するメッシュ同士をひとまとまりにする（連結成分）。

    返すのは `codes` の添字のリストで、各まとまりの中は元の順序を保つ。
    先頭のまとまりほど元の順位が高い。
    """
    idx = [meshlib.grid_index(c) for c in codes]
    parent = list(range(len(codes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a in range(len(codes)):
        for b in range(a + 1, len(codes)):
            if abs(idx[a][0] - idx[b][0]) <= 1 and abs(idx[a][1] - idx[b][1]) <= 1:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

    groups: dict[int, list[int]] = {}
    for i in range(len(codes)):
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def build_ranking(cards: list[dict], top_n: int) -> list[dict]:
    """根拠カードを、そのまま**区画単位の提言リスト**にする。

    **かつては「上位 40 区画を隣接で束ねた地区」を単位にしていた。
    2026-08-03 にやめた。** 束ね方（格子隣接）には根拠があったが、
    **どこまでを束ねるかには無かった**——地区の広がりは母数 40 で決まり、
    40 のあたりに切れ目は無い（優先度は 40 位 0.9510・60 位 0.9435 と連続）。
    「大塚・北池袋周辺は 6 区画」の 6 は場所の性質ではなく 40 で切ったことの
    帰結で、**「○○周辺」という名前が、計算していない広がりを主張していた**。
    斜めを隣とみなすか（8 近傍 / 4 近傍）にも根拠が無い。

    モデルが計算しているのは区画である。だから提言の単位も区画にする。
    **隣接は単位ではなく記述的な事実として残す**——「上位 N 区画のうち
    k 区画がこの区画に接している」と、母数を文に含めて述べる。
    母数を書けば、その数が N に依存することが読み手から隠れない。

    web/src/ui.ts の renderRanking と対。片方を触ったら必ず両方。
    """
    if not cards:
        return []

    codes = [c["mesh_code"] for c in cards]
    idx = [meshlib.grid_index(c) for c in codes]

    rows: list[dict] = []
    for i, card in enumerate(cards):
        # 上位 top_n のうち、この区画に接しているものの数（8 近傍）。
        adjacent = sum(
            1
            for j in range(len(codes))
            if j != i
            and abs(idx[i][0] - idx[j][0]) <= 1
            and abs(idx[i][1] - idx[j][1]) <= 1
        )
        rows.append(
            {
                "rank": card["rank"],
                "mesh_code": card["mesh_code"],
                "lon": card["lon"],
                "lat": card["lat"],
                "ward": card["ward"],
                "station": card["station"],
                "priority": card["priority"],
                "demand": card["demand"],
                "load": card["load"],
                "host_count": card["host_count"],
                "host_name": card["host_name"],
                "host_kind": card["host_kind"],
                "host_ward": card["host_ward"],
                "host_distance_m": card["host_distance_m"],
                "unreachable": not card["host_name"],
                "adjacent_n": adjacent,
                "adjacent_of": top_n,
                "narrative": card["narrative"] + _adjacency_note(adjacent, top_n),
            }
        )
    return rows


def _adjacency_note(adjacent: int, top_n: int) -> str:
    """隣接を「事実」として一文で述べる。**必ず母数を書く。**

    「隣接 6 区画」だけだと場所の性質に見えるが、この数は上位何件を
    見るかで変わる。母数を書けばその依存が読み手から隠れない。
    """
    if not adjacent:
        return f"上位{top_n}区画のうち、この区画に接するものは無い。"
    return f"上位{top_n}区画のうち{adjacent}区画がこの区画に接している。"


# ---------------------------------------------------------------------------
# 到達不可区画の点検
# ---------------------------------------------------------------------------


def reach_report(scored: pd.DataFrame) -> pd.DataFrame:
    """区ごとの到達不可区画と、そのうち「区内で優先度が中位以上」の件数。

    **到達不可の生の件数を区の間で並べてはいけない。** ホスト施設が
    700m 以内に無いメッシュは、23 区すべてで公共施設一覧を投入した現在、
    **データの有無ではなく非市街地の面積比でほぼ決まる**。皇居・羽田空港・
    埋立地・河川敷を含む区が上に来るだけで、「この区は転用できる施設が
    少ない」という提言にはならない（江東区の到達不可 38.8% はほぼ有明・
    青海・夢の島・若洲である）。

    そこで **区内で優先度が中位以上の到達不可区画**を数える。人の居ない
    土地は需要も負荷も低く区内で下位に沈むので、これで落ちる。
    区をまたいで並べるならこちら側を使うこと（`docs/issues.md` A9）。

    **なぜ「区内」の中位なのか。** 全体の中央値で切ると、優先度の水準が
    高い区（豊島・千代田）の到達不可だけが残り、低い区の中で相対的に
    切実な区画が消える。ここで知りたいのは「その区の中で人が居る側に
    ある到達不可」なので、閾値も区ごとに取る。

    しきい値は**区内の優先度の中央値**（境界を含む）。区内の全メッシュを
    母数にする——到達不可のメッシュだけで中央値を取ると、非市街地の多い区で
    閾値が下がり、皇居や埋立地が「中位以上」に入ってしまう。

    到達不可の判定は `f_host_n == 0`。`assign_hosts` が代表施設を
    割り当てられなかったメッシュ（`host_name` が空）と一致する。
    """
    need = {"ward", "priority", "f_host_n"}
    missing = need - set(scored.columns)
    if missing:
        raise ValueError(
            f"reach_report に必要な列が無い: {', '.join(sorted(missing))}。"
            "assign_hosts と merge した後の表を渡すこと。"
        )

    df = scored.copy()
    df["ward"] = df["ward"].fillna("").astype(str)
    df["_unreachable"] = df["f_host_n"].fillna(0).astype(float) == 0
    # 区内の中央値。母数は区内の全メッシュ（上の docstring 参照）。
    df["_median"] = df.groupby("ward")["priority"].transform("median")
    df["_mid_or_above"] = df["_unreachable"] & (df["priority"] >= df["_median"])

    rows = []
    for ward, g in df.groupby("ward"):
        # 区の判定が付かなかったメッシュ（水面など）は数えない。
        # **`TARGET_WARDS` に在るかでは絞らない。** 模擬モードの区名は
        # 「対象地域（模擬・矩形）」の 1 種類しか無く、絞ると表が空になって
        # 列ごと消え、`--report` が KeyError で落ちる（実際に落とした）。
        if not ward:
            continue
        n = len(g)
        unreachable = int(g["_unreachable"].sum())
        mid = int(g["_mid_or_above"].sum())
        rows.append(
            {
                "区": ward,
                "メッシュ": n,
                "到達不可": unreachable,
                "到達不可率": round(unreachable / n * 100, 1) if n else 0.0,
                "中位以上": mid,
                "うち中位以上": round(mid / unreachable * 100, 1) if unreachable else 0.0,
            }
        )

    # 列は明示する。0 行のときに列ごと消えると、呼び出し側が
    # 列名で触った瞬間に KeyError になる。
    cols = ["区", "メッシュ", "到達不可", "到達不可率", "中位以上", "うち中位以上"]
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values(["中位以上", "到達不可"], ascending=False, ignore_index=True)


def proximity_report(
    host_gdf: gpd.GeoDataFrame, radius_m: float = 100.0
) -> dict[str, int | float]:
    """ホストの件数を「施設の数」として読めないことを、数で示す。

    **`docs/issues.md` の独立項目（同じ建物が複数のホストとして数えられている）
    が引用してきた「595 組 / 別種別 440 組」は、算出コードがリポジトリに
    残っていなかった。** 配信データからどう数えても再現できず（100m で 459 組、
    150m で 546 組、200m で 641 組）、名寄せで 63 組を落とす前の値と辻褄が合う。
    **数字だけ直しても同じことが起きる**ので、ここで数えて `--report` に出す。

    数え方は 1 組 = 半径以内にある 2 点の組合せ（重複なし、自己組を除く）。
    「同じ建物か」は判定できないので**近いこと以上は主張しない**——
    別種別（「中町児童館」と「中町図書館」）は同じ建物である疑いが濃く、
    同一種別は表記違いの取りこぼしである疑いが濃い、という読み方までにとどめる。
    """
    pts = host_gdf.to_crs(CRS_PROJECTED)
    kinds = (
        pts["host_kind"].fillna("").astype(str).to_numpy()
        if "host_kind" in pts.columns
        else np.full(len(pts), "")
    )
    x = pts.geometry.x.to_numpy()
    y = pts.geometry.y.to_numpy()

    # x でソートして帯状に走査する。全対全（1,541² = 237 万組）でも回るが、
    # 半径を広げて確かめる使い方をするので素直に線形にしておく。
    order = np.argsort(x)
    x, y, kinds = x[order], y[order], kinds[order]

    pairs = same = 0
    n = len(x)
    for i in range(n):
        j = i + 1
        while j < n and x[j] - x[i] <= radius_m:
            if (x[j] - x[i]) ** 2 + (y[j] - y[i]) ** 2 <= radius_m**2:
                pairs += 1
                same += kinds[i] == kinds[j]
            j += 1

    return {
        "半径m": radius_m,
        "ホスト": n,
        "組": pairs,
        "同一種別": int(same),
        "別種別": pairs - int(same),
    }
