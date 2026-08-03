"""`project_audit.md` から単一ファイルの HTML を生成する。

    python tools/audit_html.py

**この生成器が要る理由。** 前回のレポートは Markdown と HTML を手で 2 つ
保っており、3 日で両方が古くなった。この作品が繰り返し踏んでいる
「同じものが 2 箇所にあると片方が黙って古くなる」型そのものである
（同じ数式が Python と TypeScript に 2 つある件と同型）。

そこで **HTML を生成物にした。** 直すのは Markdown だけでよい。

Markdown の全機能は要らない。`project_audit.md` が実際に使う記法だけを
扱う（見出し・表・箇条書き・番号付き・引用・コード・強調・リンク・水平線）。
**未対応の記法が来たら、黙って素通しせずに例外で止める**——ここも
この作品の方針に合わせてある（黙って既定値を乗せない）。
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "project_audit.md"
DST = ROOT / "project_audit.html"

CODE_SPAN = "\x00CODE%d\x00"


def inline(text: str) -> str:
    """行内の記法を HTML へ。コード span は先に退避して二重変換を防ぐ。"""
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(f"<code>{html.escape(m.group(1))}</code>")
        return CODE_SPAN % (len(spans) - 1)

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        text,
    )
    # 素の URL（リンク記法の中に入っていないもの）
    text = re.sub(
        r"(?<!\")(?<!>)(https?://[^\s<）]+)",
        lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>',
        text,
    )
    text = text.replace("——", "—<wbr>—")
    for i, span in enumerate(spans):
        text = text.replace(CODE_SPAN % i, span)
    return text


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


class Renderer:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.toc: list[tuple[int, str, str]] = []
        self.heading_n = 0

    def heading(self, level: int, text: str) -> None:
        if level == 1:
            self.out.append(f"<h1>{inline(text)}</h1>")
            return
        self.heading_n += 1
        hid = f"s{self.heading_n}"
        self.toc.append((level, hid, text))
        self.out.append(f'<h{level} id="{hid}">{inline(text)}</h{level}>')

    def table(self, rows: list[list[str]]) -> None:
        head, body = rows[0], rows[1:]
        # 見出しの無い「| | |」形式（この作品の文書が多用する）は thead を出さない
        if any(c for c in head):
            cells = "".join(f"<th>{inline(c)}</th>" for c in head)
            self.out.append('<div class="scroll"><table><thead><tr>' + cells + "</tr></thead><tbody>")
        else:
            self.out.append('<div class="scroll"><table class="headless"><tbody>')
        for row in body:
            self.out.append(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
            )
        self.out.append("</tbody></table></div>")


def render(md: str) -> str:
    r = Renderer()
    lines = md.split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # コードブロック
        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            r.out.append(f"<pre><code{cls}>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        # 水平線
        if re.fullmatch(r"-{3,}", line.strip()):
            r.out.append("<hr>")
            i += 1
            continue

        # 見出し
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            r.heading(len(m.group(1)), m.group(2))
            i += 1
            continue

        # 表（次の行が区切り行であること）
        if line.lstrip().startswith("|") and i + 1 < n and re.fullmatch(
            r"\|[\s:|-]+\|", lines[i + 1].strip()
        ):
            rows = [split_row(line)]
            i += 2
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            r.table(rows)
            continue

        # 引用
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            r.out.append("<blockquote>" + render("\n".join(buf)) + "</blockquote>")
            continue

        # 箇条書き・番号付き
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            i = render_list(r, lines, i)
            continue

        # 段落（空行まで）
        buf = []
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,4}\s|```|>|\s*([-*]|\d+\.)\s|\|)", lines[i]
        ) and not re.fullmatch(r"-{3,}", lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            r.out.append("<p>" + inline(" ".join(buf)) + "</p>")
        else:
            raise SystemExit(f"audit_html: 解釈できない行 {i + 1}: {lines[i]!r}")

    body = "\n".join(r.out)
    return body if not r.toc else wrap(body, r.toc)


def render_list(r: Renderer, lines: list[str], i: int) -> int:
    """1 段の入れ子まで対応する箇条書き。継続行は直前の項目へ足す。"""
    m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
    assert m
    base_indent = len(m.group(1))
    ordered = m.group(2)[0].isdigit()
    items: list[tuple[str, list[str]]] = []  # (本文, 入れ子の行)
    n = len(lines)

    while i < n:
        line = lines[i]
        if not line.strip():
            # 次が同じ段の項目なら続き、そうでなければ終わり
            if i + 1 < n and re.match(rf"^\s{{{base_indent}}}([-*]|\d+\.)\s+", lines[i + 1]):
                i += 1
                continue
            break
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m and len(m.group(1)) == base_indent:
            items.append((m.group(3), []))
            i += 1
            continue
        if not items:
            break
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        items[-1][1].append(line[base_indent:])
        i += 1

    tag = "ol" if ordered else "ul"
    r.out.append(f"<{tag}>")
    for text, nested in items:
        inner = ""
        if nested:
            joined = "\n".join(nested)
            if re.match(r"^\s*([-*]|\d+\.)\s+", joined.split("\n")[0]):
                inner = render(re.sub(r"^\s{2,}", "", joined, flags=re.M))
            else:
                inner = " " + inline(" ".join(x.strip() for x in nested))
        r.out.append(f"<li>{inline(text)}{inner}</li>")
    r.out.append(f"</{tag}>")
    return i


def wrap(body: str, toc: list[tuple[int, str, str]]) -> str:
    nav = "\n".join(
        f'<a class="lv{lv}" href="#{hid}">{inline(re.sub(r"\s*—.*$", "", text))}</a>'
        for lv, hid, text in toc
        if lv <= 3
    )
    return TEMPLATE.replace("{{NAV}}", nav).replace("{{BODY}}", body)


TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>calmgap-tokyo — 凍結検証レポート</title>
<style>
:root {
  --bg: #fbfaf8; --fg: #24211d; --muted: #6b6459; --line: #e2ddd4;
  --accent: #8a5a2b; --code-bg: #f2efe9; --warn-bg: #fdf6ec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171614; --fg: #e6e1d8; --muted: #9a9287; --line: #322e29;
    --accent: #d9a066; --code-bg: #221f1b; --warn-bg: #241d13;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
  line-height: 1.85; font-size: 15px;
}
.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 40px;
  max-width: 1180px; margin: 0 auto; padding: 32px 24px 96px; }
nav { position: sticky; top: 24px; align-self: start; max-height: calc(100vh - 48px);
  overflow-y: auto; font-size: 12.5px; line-height: 1.55; border-right: 1px solid var(--line);
  padding-right: 16px; }
nav a { display: block; color: var(--muted); text-decoration: none; padding: 3px 0; }
nav a:hover { color: var(--accent); }
nav a.lv3 { padding-left: 12px; font-size: 12px; }
main { min-width: 0; }
h1 { font-size: 26px; line-height: 1.4; margin: 0 0 24px; }
h2 { font-size: 20px; margin: 48px 0 16px; padding-bottom: 8px; border-bottom: 2px solid var(--line); }
h3 { font-size: 16.5px; margin: 32px 0 12px; color: var(--accent); }
h4 { font-size: 15px; margin: 24px 0 8px; }
p { margin: 12px 0; }
a { color: var(--accent); }
hr { border: 0; border-top: 1px solid var(--line); margin: 40px 0; }
code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.88em; }
pre { background: var(--code-bg); padding: 14px 16px; border-radius: 6px; overflow-x: auto;
  border: 1px solid var(--line); }
pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.65; }
blockquote { margin: 20px 0; padding: 4px 18px; border-left: 3px solid var(--accent);
  background: var(--warn-bg); border-radius: 0 4px 4px 0; }
blockquote p:first-child { margin-top: 12px; }
ul, ol { padding-left: 24px; }
li { margin: 6px 0; }
.scroll { overflow-x: auto; margin: 18px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { border: 1px solid var(--line); padding: 7px 11px; text-align: left;
  vertical-align: top; line-height: 1.65; }
th { background: var(--code-bg); font-weight: 600; white-space: nowrap; }
tbody tr:nth-child(even) { background: color-mix(in srgb, var(--code-bg) 45%, transparent); }
table.headless td:first-child { background: var(--code-bg); font-weight: 600; white-space: nowrap; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; gap: 0; }
  nav { position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--line);
    padding: 0 0 16px; margin-bottom: 24px; }
}
@media print {
  nav { display: none; }
  .layout { display: block; max-width: none; padding: 0; }
  body { font-size: 10.5pt; background: #fff; color: #000; }
  h2 { page-break-after: avoid; } table, pre, blockquote { page-break-inside: avoid; }
}
</style>
</head>
<body>
<div class="layout">
<nav>{{NAV}}</nav>
<main>
{{BODY}}
</main>
</div>
</body>
</html>
"""


def main() -> int:
    if not SRC.exists():
        sys.exit(f"{SRC} が無い")
    DST.write_text(render(SRC.read_text()), encoding="utf-8")
    print(f"{DST.relative_to(ROOT)} ({DST.stat().st_size:,} B) を {SRC.name} から生成した")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
