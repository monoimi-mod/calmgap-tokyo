"""画面に出す出典リンクが、実際にそのデータのある場所を指しているかを検査する。

    python tools/link_check.py            # 使っている出典だけ（CI はこれ）
    python tools/link_check.py --all      # 未使用の出典も含める

**この検査が要る理由。** 出典一覧は畳まずに本文へ出す方針にしてあるのに、
**リンクが正しいことを誰も確かめていなかった**。2026-08-04 に外部から
指摘を受けて 12 本すべてを叩いたところ、4 本が誤りだった:

  - `P04 医療機関` … `-v3_1` は **404**。正しくは `-v3_0`
  - `自動車騒音` … ID `t000010d0000000041` は **404**。しかも
    ラベル「自動車騒音 要請限度測定結果」は**目黒区の別データセット**の名前で、
    実際に読んでいるのは東京都環境局の「自動車交通騒音調査結果」
  - `N03 行政区域` … リンクは開けるが、**そのページの最新は 2023 年版**。
    使っている 2024 年版は別ページ（`-2024`）にある
  - `P29 学校` … 同じ。リンク先の都内版は 2013 年版しか無い

**後ろ 2 件が、この検査の形を決めている。** リンクは 200 を返し、押せば
それらしいページが開く。**HTTP 状態コードだけを見る検査では通ってしまう。**
列名の決め打ちと同じ壊れ方で（`CLAUDE.md`「列名が実在することは、それが
目的の列である証拠にならない」）、対処も同じ——**中身で裏を取る**。

  1. URL が 200 を返すこと
  2. `Source.file_hint` が空でなければ、**取得したページの中にその文字列が
     あること**。実際に読み込んだファイル名（`P29-23_13` など）を書いてある

`file_hint` が空なのは、ページから機械的に確かめる手掛かりが無い出典
（API のダウンロード画面・検索結果・ファイル名の出ない一覧ページ）。
**「空だから安全」ではなく「機械では見られないので人が見る」の意**なので、
検査は空の件数を最後に表示する。

未使用の出典（`layer is None`）は画面に出ないので既定では見ない。
`--all` で見る。URL が空文字のものは「出典が存在しないこと自体が所見」
（`tokyo_rail_noise`）なので、どちらのモードでも飛ばす。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# `python tools/link_check.py` で走らせると sys.path に載るのは tools/ なので、
# リポジトリのルートを足す（他の検査は etl を import していないため前例が無い）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.config import SOURCES  # noqa: E402

# ブラウザの UA を名乗る。国土数値情報も都のカタログも、既定の
# `Python-urllib/3.x` には応答を変える（都のカタログは AWS WAF の
# チャレンジ HTML を 202 で返し、**200 でも 404 でもない**）。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 30


# 都のオープンデータカタログ（CKAN）。**HTML は AWS WAF の裏にいる**が、
# **API は素通しで、しかも 404 を JSON で正しく返す**。HTML を見に行くと
# チャレンジページが 202 で返り、生きているのか死んでいるのかが判別できない
# ——`t000010d0000000041` の 404 を 3 か月見落としたのはこれが理由でもある。
# データセットのリンクは API 側で確かめる。
CKAN_DATASET = re.compile(
    r"^https://catalog\.data\.metro\.tokyo\.lg\.jp/dataset/([0-9a-zA-Z_-]+)$"
)
CKAN_API = "https://catalog.data.metro.tokyo.lg.jp/api/3/action/package_show?id="

# AWS WAF のチャレンジ。200 でも 202 でも返り得るので、状態コードではなく
# 中身で見分ける。**通してはいけない**——ページの中身ではないので、
# file_hint の照合は必ず落ちるが、理由が「無い」ではなく「見られていない」になる。
WAF_MARKERS = ("awsWafCookieDomainList", "gokuProps", "challenge.js")


def encode(url: str) -> str:
    """URL の非 ASCII を percent-encode する（`?q=公共施設` がそのまま来る）。"""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(parts.path, safe="/%"),
            urllib.parse.quote(parts.query, safe="=&%"),
            parts.fragment,
        )
    )


def fetch(url: str) -> tuple[int, str]:
    """(状態コード, 本文) を返す。取得できなければ本文は空。"""
    req = urllib.request.Request(
        encode(url),
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "ja,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            body = res.read()
            return res.status, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # DNS・TLS・タイムアウト
        print(f"    取得できない: {e}", file=sys.stderr)
        return 0, ""


def check_ckan(dataset_id: str, file_hints: tuple[str, ...]) -> tuple[bool, str]:
    """CKAN の API でデータセットの実在と、収録ファイル名を確かめる。

    **`success: true` だけでは足りない。** リンクが生きていても別の
    データセットを指していることがある（騒音のラベルは目黒区の
    「自動車騒音要請限度測定結果」で、実際に読んでいるのは環境局の
    「自動車交通騒音調査結果」だった）。リソースの URL に
    `file_hint`（`H25_kekka.csv`）が含まれることまで見る。
    """
    status, body = fetch(CKAN_API + dataset_id)
    if status != 200 or not body:
        return False, f"API が HTTP {status}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False, "API の応答が JSON ではない（WAF の可能性）"
    if not payload.get("success"):
        msg = payload.get("error", {}).get("message", "")
        return False, f"データセットが存在しない（{msg or 'Not Found'}）"

    result = payload.get("result", {})
    title = result.get("title", "")
    if not file_hints:
        return True, f"実在する: 「{title}」"

    urls = " ".join(
        str(r.get("url", "")) + " " + str(r.get("name", ""))
        for r in result.get("resources", [])
    )
    # **書いた手掛かりは全部あること。** 1 つでも欠けたら落とす
    # （複数年度を 1 ページから取る出典で、古い年度だけ消えても通ってしまう）。
    missing = [h for h in file_hints if h not in urls]
    if not missing:
        return True, f"「{title}」に {'・'.join(file_hints)} がある"
    return False, f"「{title}」に {'・'.join(missing)} が無い"


def main(argv: list[str]) -> int:
    include_unused = "--all" in argv

    wanted = [s for s in SOURCES.values() if include_unused or s.layer is not None]
    targets = [s for s in wanted if s.url]

    failures: list[str] = []
    unverified: list[str] = []  # 到達はしたが、中身で裏を取れなかったもの

    # **URL が無い出典を黙って落とさない。** `s.url` で絞っていたため、
    # 手集計の出典（既存カームダウンスペース）が**使用中なのに検査の対象から
    # 静かに外れていた**——「検査 13 件」と出るのに使用中は 14 件、という
    # 状態で、数を数えないと気付けない。**検査できないことと、検査対象に
    # 入っていないことは違う。** 人が確かめる側の一覧に載せる。
    for src in wanted:
        if src.url:
            continue
        print(f"[使用中] {src.label}\n    URL なし（{src.kind}）")
        unverified.append(f"{src.key}（URL の無い出典。中身は人が確かめる）")

    for src in targets:
        used = "使用中" if src.layer else "未使用"
        print(f"[{used}] {src.label}\n    {src.url}")

        # --- 都のカタログのデータセットは API で見る ---
        m = CKAN_DATASET.match(src.url)
        if m:
            ok, why = check_ckan(m.group(1), src.file_hints)
            print(f"    {'✓' if ok else '✗'} {why}")
            if not ok:
                failures.append(f"{src.key}: {why} — {src.url}")
            elif not src.file_hint:
                unverified.append(f"{src.key}（file_hint が空）")
            continue

        status, body = fetch(src.url)

        # **WAF のチャレンジは状態コードで見分けられない**（202 で来る）ので
        # 中身で見る。**「200 だから通す」にしてはいけない**——チャレンジは
        # ページの中身ではないので、合格にすると「リンク切れでも通る」検査に
        # 戻る。かといって失敗にもできない（bot 対策の挙動で CI が落ちる）。
        # 落とさず、**確認できなかったと言って一覧に残す**。
        if any(mark in body for mark in WAF_MARKERS):
            unverified.append(f"{src.key}（bot 対策で機械的に見られない）")
            print(f"    △ HTTP {status}・チャレンジページ。人が押して確かめること")
            continue

        # 鍵の要る API のエンドポイントは、鍵無しで 401/403 を返すのが正常。
        # これを失敗にすると「認証が要る」と「消えた」の区別が付かなくなる。
        # （画面に出す出典としては API のエンドポイントを指すべきではないが、
        # レジストリには取得先として残る。`--all` でしか見えない。）
        if status in (401, 403) and src.api_key_env:
            unverified.append(f"{src.key}（鍵の要る API・HTTP {status}）")
            print(f"    △ HTTP {status}（{src.api_key_env} が要る）")
            continue

        if status != 200:
            failures.append(f"{src.key}: HTTP {status} — {src.url}")
            print(f"    ✗ HTTP {status}")
            continue

        if not src.file_hint:
            # **「書けない」と「まだ書いていない」を区別する。**
            # 空欄だけを見て「file_hint が空」と言っていたため、
            # 都教委の在籍者数が**確かめずに書いた理由**（「一覧ページに
            # ファイル名は出ていない」——出ていた）で 1 件ぶん緩いまま
            # 残っていた。理由が書いてあるものは「試して駄目だった」、
            # 空のものは「まだ試していない」である。
            if src.unverifiable:
                unverified.append(f"{src.key}（{src.unverifiable}）")
                print(f"    △ HTTP 200・照合できない理由あり: {src.unverifiable}")
            else:
                unverified.append(f"{src.key}（file_hint が空・理由も未記入）")
                print("    ○ HTTP 200（中身の照合は無し。file_hint が空）")
            continue

        missing = [h for h in src.file_hints if h not in body]
        if not missing:
            found = "」「".join(src.file_hints)
            print(f"    ✓ HTTP 200 / 「{found}」がページ内にある")
        else:
            # **ここが本題。** リンクは生きているのに、使ったデータが無い。
            lack = "」「".join(missing)
            failures.append(
                f"{src.key}: リンクは 200 だが「{lack}」がページに無い — {src.url}"
            )
            print(f"    ✗ 「{lack}」がページ内に無い（{len(src.file_hints)} 件中）")

    print()
    print(
        # **母数は wanted（対象の出典すべて）で数える。** targets（URL のあるもの）で
        # 割ると、URL の無い出典が分母からも消えて辻褄が合ってしまう。
        f"検査 {len(wanted)} 件（うちリンクを開いたもの {len(targets)} 件）/ "
        f"中身まで裏を取れたもの {len(wanted) - len(unverified)} 件"
    )
    if unverified:
        # 隠さない。ここは人が押して確かめるしかない出典である。
        print("中身の照合ができなかったもの（人が確かめる）:")
        for u in unverified:
            print(f"  - {u}")

    if failures:
        print()
        print("--- 失敗 ---")
        for f in failures:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
