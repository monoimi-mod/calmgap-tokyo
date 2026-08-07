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

## 提出物（スライド・台本・記入案）は向きが逆である

`docs/status.md` は**現在値がすべて書かれている**ことを求める文書なので、
「配信データの値が文書に在るか」を見れば足りる。

**提出物はそうではない。** スライドが引くのは数値の一部だけで、
全部を求めると「区平均の開き 4.0dB がスライドに無い」で落ちる。
必要なのは逆向きの検査——**その資料が引いている数値が、いまのビルドと
合っているか**である。だから資料ごとに「載っている値」を明示的に
並べる（`SUBMISSION_CHECKS`）。

**資料から主張を消したら、この一覧からも消すこと。** 一覧に残ったまま
資料から消えると落ちる。それは正しい落ち方で、**資料を直したのに
一覧を直していない**ことを指している。

**配信データから出せない数を資料に書かないこと。** 「プリセットごとに
133〜171 件」は 2026-08-06 に手で測った値で、`meta.json` にも
`sensitivity.json` にも無い。検査できないうえ、ビルドが変われば黙って
古くなる——画面が同じ理由でこれを出していないので、スライドからも外した
（2026-08-07）。**出したいなら先に ETL から配信すること。**
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DATA = ROOT / "web" / "public" / "data"

# 現況の数値を書く場所。ここに現在値が無ければ反映漏れとみなす。
STATUS = ROOT / "docs" / "status.md"

# 提出物。**審査員が読むのはこちらで、status.md は読まれない。**
SLIDES = ROOT / "docs" / "slides.html"
SCRIPT = ROOT / "docs" / "presentation.md"
FORM = ROOT / "docs" / "submission-form.md"


def load(name: str):
    path = WEB_DATA / name
    if not path.exists():
        sys.exit(
            f"{path.relative_to(ROOT)} が無い。"
            "先に `python -m etl.build --live --sensitivity` を実行すること。"
        )
    return json.loads(path.read_text())


def read_doc(path: Path) -> str:
    """文書を読み、突き合わせる前に表記を寄せる。

    **マイナス記号。** 文書は全角相当の U+2212（−0.632）で書き、Python の
    書式は ASCII のハイフン（-0.283）を出す。**見た目が同じで一致しない**
    ——この検査でいちばん出したくない偽陽性なので片側へ正規化する
    （文書側の書き方は変えない）。

    **HTML の改行。** `slides.html` は 1 つの数値が
    `135,911 人<br />／日` のようにタグで割れることがある。タグを
    落としてから突き合わせる——**落とさないと「資料に書いてあるのに
    無い」と言う検査**になり、直しようがない指摘が出る。
    """
    text = path.read_text()
    if path.suffix == ".html":
        text = re.sub(r"<[^>]+>", "", text)
    return squash(text.replace("−", "-"))


def squash(s: str) -> str:
    """空白を落とす。**単位の前の空白は書き手の自由にする。**

    同じ値が資料ごとに `67.6dB` と `67.6 dB`、`10レイヤー` と `10 レイヤー` の
    両方で書かれている。**どちらも正しい**——この検査は文書の書き方を
    縛らない方針なので、突き合わせる前に両側から空白を落とす。

    `slides.html` では 1 つの数値がタグで割れる（`72.1 <small>%</small>`）。
    タグを落とすと `72.1 %` が残るので、ここでも同じ処理で吸収される。

    **数字の途中に当たる誤判定は増えない**——`contains` は直前の文字が
    数字・カンマ・小数点かどうかで見ており、空白を落としても
    `44件` の前が `4` であることは変わらない。
    """
    return re.sub(r"[\s　]+", "", s)


def contains(text: str, value: str) -> bool:
    """`value` が `text` に在るか。**数字の途中に当たったら在ると見なさない。**

    素の部分一致だと、**別の数の末尾に当たって黙って通る**。実際に踏んだ:
    selftest が 71 件になったとき、検査は ok を出したが status.md は
    69 件のままで、当たっていたのは `高齢系 971 件` の中の「71 件」だった。
    **検査が通ったのに文書は古い**——この道具がいちばん出してはいけない結果。

    直前の文字が数字・カンマ・小数点なら、その一致は別の数の一部である。
    書式には踏み込まない（この検査は文書の書き方を縛らない方針）。

    `text` は `read_doc` が空白を落としたもの。**期待値の側も同じ規則を
    通す**——片側だけだと「67.6 dB」が永久に一致しない。
    """
    value = squash(value)
    start = 0
    while (i := text.find(value, start)) != -1:
        if i == 0 or text[i - 1] not in "0123456789,.":
            return True
        start = i + 1
    return False


def pct(x: float) -> str:
    """0.7334 → '73.3%' / 0.65 → '65%'。文書の書き方に合わせる。

    **組み込みの `round` を使わない。** 「ちょうど半分」を偶数側へ
    丸めるため 75.25 が 75.2 になる（`CLAUDE.md` の丸めの項と同じ理由）。
    文書に書くのは大きい側——`score.publish_round` と規則を揃える。
    """
    v = Decimal(str(x * 100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{v.normalize():f}%"


def d3(x: float) -> str:
    """0.9889 → '0.989'。画面が出すのと同じ 3 桁に丸める。

    **組み込みの `round` を使わない**（`CLAUDE.md` の丸めの項）。
    """
    return f"{Decimal(str(x)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP):f}"


def submission_checks(meta: dict, sens: dict | None, order: list[dict]) -> list[tuple[Path, str, str]]:
    """提出物が引いている数値を、いまのビルドから組み立てる。

    **1 位の区画を決め打たない。** 優先度が最大の区画を取り、その区名・
    最寄り駅名・メッシュコードも検査対象に入れる。上位が入れ替わったら
    資料の側が落ちる——**それが起きてほしい落ち方**である
    （特別支援学校の規模を入れただけで提言 8 件のうち 4 件が入れ替わった、
    という履歴のある作品なので、上位はいつでも動く前提で組む）。

    **実数は配信された `f_*` から採る。** これは `demand_points.geojson`
    から数え直したものと全区画で一致することを `tools/facility_parity.mjs`
    が検査しているので、ここで数え直すと**同じ規則の 3 本目の実装**に
    なってしまう（`CLAUDE.md` が繰り返し警告している型）。
    """
    top = order[0]
    u = meta["unreachable"]
    score_layers = sum(1 for r in meta["layer_roles"] if r["role"] == "score")

    # **模擬データでは資料の数値を検査できない。** 区名・駅名・外部照合は
    # 実データ側にしか無いので、**無いことを黙って飛ばさずに止める**
    # （飛ばすと「提出物 ok」と出たまま何も見ていない状態になる）。
    if meta.get("synthetic") or "calm_spaces" not in meta:
        sys.exit(
            "提出物の検査は実データのビルドでのみ行える。"
            "`python -m etl.build --live --sensitivity` を実行すること"
        )
    for key in ("f_station_name", "f_zoning_name", "f_welfare_n", "f_welfare_cap"):
        if top.get(key) is None:
            sys.exit(f"1 位の区画 {top['c']} に {key} が無い。配信データが想定と違う")

    ward = meta["target_wards"][top["w"]]
    cs = meta["calm_spaces"]
    rooms = sum(s["rooms"] for s in cs["sites"])
    ranks = sorted(s["rank"] for s in cs["sites"])
    median_rank = ranks[len(ranks) // 2]
    by_access: dict[str, int] = {}
    for s in cs["sites"]:
        by_access[s["access"]] = by_access.get(s["access"], 0) + s["rooms"]

    # (ファイル, 説明, 在るべき文字列)
    out: list[tuple[Path, str, str]] = []

    def need(files: tuple[Path, ...], label: str, value: str) -> None:
        for f in files:
            out.append((f, label, value))

    ALL = (SLIDES, SCRIPT, FORM)

    # --- 規模。3 つの資料すべてが名乗っている ---
    need(ALL, "メッシュ数", f"{meta['mesh_count']:,}")
    need((SLIDES, FORM), "データの層", f"{meta['layer_total']} レイヤー")
    need((SLIDES,), "スコアに入る層", f"{score_layers} 層")

    # --- 記入案 4-1 の備考欄が騒音の数を引いている。**年度を足すたびに動く** ---
    need((FORM,), "騒音の測定点", f"{meta['layer_counts']['noise']:,} 点")
    need(
        (FORM,),
        "騒音の観測圏外セル",
        f"{sum(1 for r in order if not r.get('f_noise_n')):,} 区画",
    )

    # --- 1 位の区画。**スライドと台本が実数を並べている行** ---
    need((SLIDES, SCRIPT), "1 位のメッシュコード", top["c"])
    need((SLIDES, SCRIPT), "1 位の区", ward)
    need((SLIDES, SCRIPT), "1 位の最寄り駅", top["f_station_name"])
    need((SLIDES,), "1 位の優先度", d3(top["priority"]))
    need((SLIDES,), "1 位の需要", d3(top["demand"]))
    need((SLIDES,), "1 位の負荷", d3(top["load"]))
    need((SLIDES, SCRIPT), "1 位の事業所件数", f"{top['f_welfare_n']} 件")
    need((SLIDES,), "1 位の事業所定員計", f"{top['f_welfare_cap']:,} 人")
    need((SLIDES,), "1 位の精神科件数", f"{top['f_clinic_n']} 件")
    need((SLIDES, SCRIPT), "1 位の駅の乗降計", f"{top['f_station_sum']:,} 人")
    need((SLIDES, SCRIPT), "1 位の用途地域", top["f_zoning_name"])
    need((SLIDES, SCRIPT), "1 位の推定騒音", f"{top['f_noise_db']} dB")

    # --- 2 つの出力 ---
    need(ALL, "到達不可のうち区内で中位以上", f"{u['mid_or_above']} 区画")
    need((SLIDES,), "提言の既定件数", f"上位 {meta['ranking_default_n']} 区画")
    need((SLIDES,), "徒歩圏の半径（供給側）", f"{meta['fact_radius_m']['host']:.0f}m")

    # --- 外部照合。**この作品で唯一、外の物差しで確かめた部分** ---
    need((SLIDES, SCRIPT), "既存の設置か所", f"{len(cs['sites'])} か所")
    need((SLIDES, SCRIPT), "既存の設置室数", f"{rooms} 室")
    need((SLIDES, SCRIPT), "既存の設置の順位の中央値", f"{median_rank:,} 位")
    need((SLIDES,), "その場で使える室数", f"{by_access.get('open', 0)} 室")
    need((SLIDES,), "関係者のみの室数", f"{by_access.get('members', 0)} 室")
    need((SLIDES,), "保安検査後の室数", f"{by_access.get('airside', 0)} 室")
    need((SLIDES,), "有料の室数", f"{by_access.get('ticketed', 0)} 室")

    # --- 順位の不安定さ。**ここを落とすと資料が作品より強く見える** ---
    if sens:
        pa = sens["preset_agreement"]
        need((SLIDES,), "プリセット共通の母数", f"上位 {pa['top_k']} 件")
        need((SLIDES,), "重み ±30% の上位10", pct(sens["random_perturbation"]["overlap_mean"]["10"]))
        groups = {g["id"]: g for g in sens["fixed_values"]["groups"]}
        need((SLIDES,), "揺さぶった固定値の数", f"{groups['all']['constants']} 個")
        need((SLIDES,), "重みの数", f"重み {len(meta['components'])} 個")
    need((SLIDES,), "特別支援学校の校数", f"{meta['layer_counts']['schools']} 校")

    return out


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

    # 区ダミーとの相関（`docs/issues.md` A1）。**この検査が最後まで無かった。**
    # A1 は騒音 × 世田谷区の相関を 5 ビルド並べてきたのに、その数値を出す
    # 経路がコードの側に無く、文書に貼った再現スクリプトだけが頼りだった。
    # しかもそのスクリプトは区の割り当てが本体と違い（重心 within 対 面積優占）、
    # **文書の値をその文書の手順では再現できなかった**（+0.2477 対 +0.2486）。
    #
    # **2 つとも見る。** tracked は決め打ちの組で、経緯の比較用。
    # components は区を決め打たない側で、**偏りが別の区へ移ったことに
    # 気付くため**にある——実際いま騒音の最大は世田谷区ではなく練馬区で、
    # この検査を入れるまで文書はそれを一度も書いていなかった。
    wd = meta.get("ward_dependence")
    if wd:
        comps = {c["key"]: c for c in wd.get("components", [])}
        tracked = wd.get("tracked")
        if tracked:
            checks.append(
                (f"A1 の組（{tracked['key']} × {tracked['ward']}）", f"{tracked['corr']:+.3f}")
            )
        if "noise" in comps:
            c = comps["noise"]
            checks.append(("騒音の区依存が最大の区", c["ward"]))
            checks.append(("騒音の区依存が最大の相関", f"{c['corr']:+.3f}"))
        # 8 層の中で最も強く 1 つの区に張り付いている層。**騒音とは限らない**
        # ——いまは用途地域で、A1 が騒音の話として書いてきたことの外にある。
        if wd.get("components"):
            top = wd["components"][0]
            checks.append(("区依存が最大の層", top["label"]))
            checks.append(("区依存が最大の層の相関", f"{top['corr']:+.3f}"))
        nwm = wd.get("noise_ward_mean")
        if nwm:
            # 「区ごとに 4.0dB 開く」も同じ再現スクリプトの中で手計算されていた
            checks.append(("騒音の区平均の下端", f"{nwm['min_db']:.1f}dB"))
            checks.append(("騒音の区平均の上端", f"{nwm['max_db']:.1f}dB"))
            checks.append(("騒音の区平均の開き", f"{nwm['spread_db']:.1f}dB"))
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

    text = read_doc(STATUS)

    failures = [
        f"{label}: 現在値 {value!r} が {STATUS.relative_to(ROOT)} に無い"
        for label, value in checks
        if not contains(text, value)
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
        mark = "ok  " if contains(text, value) else "FAIL"
        print(f"  {mark}  {label:<28} {value}")

    # ------------------------------------------------ 提出物（審査員が読む側）
    #
    # **status.md より優先度が高い。** status.md は作業再開の起点で、
    # 古くなっても直せる。**提出物は 8月23日17時に固定される。**
    subs = submission_checks(meta, sens, order)
    docs = {path: read_doc(path) for path in {p for p, _, _ in subs}}
    print("\n提出物の数値と配信データの照合")
    for path in sorted(docs, key=lambda p: p.name):
        rows = [(lab, val) for p, lab, val in subs if p == path]
        bad = sum(1 for lab, val in rows if not contains(docs[path], val))
        head = "ok  " if not bad else "FAIL"
        print(f"  {head}  {path.relative_to(ROOT)}  {len(rows)} 項目"
              + (f" / 食い違い {bad} 件" if bad else ""))
        for lab, val in rows:
            if not contains(docs[path], val):
                print(f"          - {lab}: 現在値 {val!r} が無い")
    failures += [
        f"{path.relative_to(ROOT)} — {label}: 現在値 {value!r} が無い"
        for path, label, value in subs
        if not contains(docs[path], value)
    ]

    if failures:
        print(f"\n{len(failures)} 件の食い違い\n")
        for f in failures:
            print(f"  {f}")
        # **落ちたときに直すのは資料であって、この検査ではない。**
        # 一覧から消すのは「資料からその主張ごと消した」ときだけ。
        print(
            "\n提出物の側が落ちたときは、資料の数値を新しいビルドに合わせること。"
            "\n主張ごと消した場合だけ、tools/doc_numbers.py の一覧からも消す。"
        )
        return 1
    print("\nすべて一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
