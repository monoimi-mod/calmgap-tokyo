"""現況の数値が、配信データと合っているかを検査する。

    python tools/doc_numbers.py

**この検査が要る理由。** `CLAUDE.md` は「過去の数字を引用しないこと」と
繰り返し戒めているのに、**それを機械的に守る仕組みが無かった**。
実際、短期間に 4 回踏んでいる:

  - `selftest 45 件` … 実際は 58 件（増やしたときに文書を直し忘れた）
  - `到達不可のうち中位以上 128 件` … 優先度が変わって 151 件になっていた
  - `需要寄与の 68.9% が仮定員` … 判定を直したら 25.1% だった
  - 感度分析のラベル `徒歩圏の帯域（800m / 1200m / 600m）` … 直した後も表示だけ残った

いずれも**間違っていても動く**種類の誤りで、ビルドもテストも成功と表示される。

**この検査を入れた後も 12 箇所踏んだ**（2026-08-03 の凍結検証。`project_audit.md`）。
網が `meta.json` 由来のスカラーだけで、**`sensitivity.json` を 1 つも見ていなかった**
ためで、帯域を 800m へ直して測り直した値が文書側に反映されていなかった。
感度分析の主要な値もここで検査する。

**さらに、この検査自身が誤りを固定していた**——渋谷駅前として
渋谷駅の 700m 南（代官山）のメッシュを見ており、その順位に合わせて
文書 4 本が書き換えられていた。**コードだけでなく中身で裏を取る**（下記）。

**「古い値が残っていないか」は検査しない。** この作品の文書は
過去の数値を意図的に残している——「かつて 44.4% だった」「A4 是正前の
渋谷駅前 519 位」のように、**入れ替わってきた経緯そのものが中身**だからである。
古い値の出現を機械的に禁じると、経過の記録と、消し忘れた現況とが
区別できずに偽陽性だらけになる（実際そう書いてみて 17 件中 16 件が
偽陽性だった。「上位 50 件」が selftest の「50 件」に、
足立区の「その 45 件」が「45 件」に当たる）。

代わりに **`docs/status.md`（現況まとめ）に現在値が書かれていること**だけを
見る。ここは「作業再開の起点」と決めてある文書で、経過ではなく現在の状態を
書く場所なので、現在値が無ければそれは反映漏れである。

新しい指標を出したらここへ足すこと。足さなければ守られない。
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DATA = ROOT / "web" / "public" / "data"

# 現況の数値を書く場所。ここに現在値が無ければ反映漏れとみなす。
STATUS = ROOT / "docs" / "status.md"


def load(name: str):
    path = WEB_DATA / name
    if not path.exists():
        sys.exit(
            f"{path.relative_to(ROOT)} が無い。"
            "先に `python -m etl.build --live --sensitivity` を実行すること。"
        )
    return json.loads(path.read_text())


def selftest_count() -> int:
    """selftest の検査項目数を、実際に走らせて数える。"""
    out = subprocess.run(
        [sys.executable, "-m", "etl.selftest"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return sum(1 for line in out.stdout.splitlines() if line.startswith("  ok"))


def main() -> int:
    meta = load("meta.json")
    proposals = load("proposals.json")
    mesh = load("mesh.geojson")
    rows = [f["properties"] for f in mesh["features"]]
    u = meta["unreachable"]

    order = sorted(rows, key=lambda r: -r["priority"])
    # **このメッシュコードを目視で変えないこと。** 5339358633 は渋谷駅（35.6580,
    # 139.7016）を含む区画で、配信データ側の f_station_name が「渋谷」・
    # 用途地域が「商業地域」・乗降 2,897,703 人/日 になっていることで裏が取れる。
    # かつて 5339358611 を渋谷駅前として検査していた——実際には約 700m 南の
    # 代官山（第二種低層住居専用地域・乗降 28,772 人）で、その 3,469 位という
    # 順位に合わせて文書が 4 本書き換えられていた。**検査が誤りを固定していた。**
    # 下の裏取りは、次に誰かがコードを差し替えたときに黙って通らないためにある。
    shibuya = None
    for i, r in enumerate(order):
        if r["c"] != "5339358633":
            continue
        if r.get("f_station_name") != "渋谷" or r.get("f_zoning_name") != "商業地域":
            sys.exit(
                "渋谷駅前として検査しているメッシュ 5339358633 の中身が想定と違う"
                f"（最寄り駅 {r.get('f_station_name')!r} / 用途地域 {r.get('f_zoning_name')!r}）。"
                "メッシュコードか配信データのどちらかが変わっている"
            )
        shibuya = i + 1
        shibuya_priority = r["priority"]
        break

    # **提言リストの件数は config の値そのものなので、文書との突き合わせでは
    # 意味がない**（"50 件" は他の行にも当たる）。代わりに配信 JSON どうしの
    # 整合として見る——proposals.json が既定件数ぶん書き出されていること。
    if len(proposals) != meta["ranking_default_n"]:
        sys.exit(
            f"proposals.json の件数 {len(proposals)} が "
            f"meta.ranking_default_n {meta['ranking_default_n']} と違う"
        )

    # **到達不可がどのあたりの順位にいるか。** 2026-08-04 に見出し数値を
    # 1,058 から 151 へ格下げした根拠がこれで、status.md がその数字を
    # 引用している。**「上位 100 区画には 1 件も入らない」は重みで動く**
    # ——既定重みでの値なので、重みの既定値を変えたらここが落ちる。
    # 落ちたときに直すのは status.md であって、この検査ではない。
    unreachable_ranks = [i + 1 for i, r in enumerate(order) if not r.get("host")]
    rank_median = sorted(unreachable_ranks)[len(unreachable_ranks) // 2]
    in_top100 = sum(1 for r in unreachable_ranks if r <= 100)

    # 需要側の点のうち、規模が仮の値のもの。**この数を配信するようにしたので
    # 検査に入れる**（学校 3 件は筑波大附属で、在籍者数を公表していない）。
    demand_points = load("demand_points.geojson")
    assumed = [
        f["properties"] for f in demand_points["features"] if f["properties"].get("assumed")
    ]

    # (説明, status.md に在るべき文字列)
    checks: list[tuple[str, str]] = [
        ("メッシュ数", f"{meta['mesh_count']:,}"),
        ("実データのレイヤー数", f"{meta['real_layer_count']} / {meta['layer_total']}"),
        ("ホスト施設", f"{meta['layer_counts']['hosts']:,}"),
        ("障害福祉事業所", f"{meta['layer_counts']['welfare']:,}"),
        ("到達不可の件数", f"{u['count']:,}"),
        ("到達不可の率", f"{u['ratio'] * 100:.1f}%"),
        ("到達不可のうち区内で中位以上", f"{u['mid_or_above']} 件"),
        ("到達不可の順位の中央値", f"{rank_median:,} 位"),
        ("到達不可のうち上位100区画", f"{in_top100} 件"),
        ("規模が仮の値の点", f"{len(assumed):,} 件"),
        ("selftest の件数", f"{selftest_count()} 件"),
    ]
    if shibuya is not None:
        checks.append(("渋谷駅前の順位", f"{shibuya:,} 位"))
        # **優先度の値も見る。** 2026-08-03 に最後のパーセンタイル化を外して
        # 「需要 × 負荷」の生値を配信するようにしたとき、順位（792 位）は
        # 動かないのに値だけが 0.915 → 0.7219 へ変わった。順位しか検査して
        # いなかったため、文書側の 0.915 は検査を素通りしていた。
        checks.append(("渋谷駅前の優先度", f"{shibuya_priority}"))

    # 感度分析の値も見る。**ここが最後まで検査の外にあった。**
    # 帯域を 1,200m → 800m へ直したとき全部を測り直したのに、追随したのは
    # issues.md A4 の比較表と status.md の一部だけで、**12 箇所が 1,200m の値のまま
    # 残った**（2026-08-03 の凍結検証。`project_audit.md` D-1〜D-4）。
    # 変化がいちばん大きく、「不利な結果」として最も引用される数値群が、
    # meta.json 由来のスカラーしか見ない検査からこぼれていた。
    sens_path = WEB_DATA / "sensitivity.json"
    sens = json.loads(sens_path.read_text()) if sens_path.exists() else None
    if sens:
        rp = sens["random_perturbation"]
        fv = sens["fixed_values"]
        groups = {g["id"]: g for g in fv["groups"]}
        loo = {r["key"]: r for r in sens["leave_one_out"]}
        scen = {s["id"]: s for s in fv["scenarios"]}
        # bandwidth_profile は fixed_values の下にある（トップレベルではない）
        band = {b["key"]: b for b in fv.get("bandwidth_profile", [])}

        def pct(x: float) -> str:
            """0.7334 → '73.3%' / 0.65 → '65%'。文書の書き方に合わせる。

            **組み込みの `round` を使わない。** 「ちょうど半分」を偶数側へ
            丸めるため 75.25 が 75.2 になる（`CLAUDE.md` の丸めの項と同じ理由）。
            文書に書くのは大きい側——`score.publish_round` と規則を揃える。
            """
            v = Decimal(str(x * 100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            return f"{v.normalize():f}%"

        checks += [
            ("重み ±30% の上位10", pct(rp["overlap_mean"]["10"])),
            ("固定値 76 個の上位10", pct(groups["all"]["overlap_mean"]["10"])),
            ("帯域群の上位10", pct(groups["bandwidth"]["overlap_mean"]["10"])),
            ("特別支援学校の LOO", pct(loo["sped_school"]["overlap_top10"])),
            ("用途地域の LOO", pct(loo["zoning"]["overlap_top10"])),
            ("仮定員を全部落とす", pct(scen["drop_assumed_capacity"]["overlap"]["10"])),
            ("プリセット共通", f"{sens['preset_agreement']['common_count']} 件"),
        ]
        if "sped_school" in band:
            checks.append(
                ("帯域・特別支援学校の単独", pct(band["sped_school"]["alone"]["overlap_mean"]["10"]))
            )

    text = STATUS.read_text()
    failures = [
        f"{label}: 現在値 {value!r} が {STATUS.relative_to(ROOT)} に無い"
        for label, value in checks
        if value not in text
    ]

    # 画面の staleness 検知と同じ条件。配信物どうしの整合。
    if sens and sens.get("generated_at") != meta["generated_at"]:
        failures.append(
            "sensitivity.json が meta.json と別のビルド"
            f"（{sens.get('generated_at')} / {meta['generated_at']}）。"
            "`python -m etl.build --live --sensitivity` で作り直すこと"
        )

    print(f"現況の数値と配信データの照合（{STATUS.relative_to(ROOT)}）")
    for label, value in checks:
        mark = "ok  " if value in text else "FAIL"
        print(f"  {mark}  {label:<28} {value}")

    if failures:
        print(f"\n{len(failures)} 件の食い違い\n")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nすべて一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
