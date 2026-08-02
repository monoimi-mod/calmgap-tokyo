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
    shibuya = next((i + 1 for i, r in enumerate(order) if r["c"] == "5339358611"), None)

    # (説明, status.md に在るべき文字列)
    checks: list[tuple[str, str]] = [
        ("メッシュ数", f"{meta['mesh_count']:,}"),
        ("実データのレイヤー数", f"{meta['real_layer_count']} / {meta['layer_total']}"),
        ("ホスト施設", f"{meta['layer_counts']['hosts']:,}"),
        ("障害福祉事業所", f"{meta['layer_counts']['welfare']:,}"),
        ("到達不可の件数", f"{u['count']:,}"),
        ("到達不可の率", f"{u['ratio'] * 100:.1f}%"),
        ("到達不可のうち区内で中位以上", f"{u['mid_or_above']} 件"),
        ("提言の件数", f"{len(proposals)} 件"),
        ("selftest の件数", f"{selftest_count()} 件"),
    ]
    if shibuya is not None:
        checks.append(("渋谷駅前の順位", f"{shibuya:,} 位"))

    text = STATUS.read_text()
    failures = [
        f"{label}: 現在値 {value!r} が {STATUS.relative_to(ROOT)} に無い"
        for label, value in checks
        if value not in text
    ]

    # 画面の staleness 検知と同じ条件。配信物どうしの整合。
    sens_path = WEB_DATA / "sensitivity.json"
    if sens_path.exists():
        sens = json.loads(sens_path.read_text())
        if sens.get("generated_at") != meta["generated_at"]:
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
