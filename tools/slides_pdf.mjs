/**
 * 提出用スライド（5-1）を `docs/slides.html` から PDF へ書き出す。
 *
 *   node tools/slides_pdf.mjs        → docs/slides.pdf
 *
 * **PPTX にしていない。** 日本語のスライドは、相手の環境にフォントが
 * 無いと行が折り返して**レイアウトごと崩れる**——しかも作った側の画面では
 * 正しく見えるので、崩れていることに気付けない。PDF は字形を埋め込むので、
 * 審査員が開いた画面と手元の画面が同じものになる。
 * （提出フォーム 5-1 は PowerPoint / PDF のどちらでも可。）
 *
 * 原稿が HTML なので、**数値の変更が diff に出る**。この作品は
 * 「過去の数字を引用しない」を機械で守る仕組みを持っているので、
 * 原稿をテキストに置いておく意味がそこにある。
 */
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SRC = resolve("docs/slides.html");
const OUT = resolve("docs/slides.pdf");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const port = 9722 + Math.floor(Math.random() * 400);
const chrome = spawn(CHROME, [
  "--headless=new",
  "--hide-scrollbars",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=/tmp/calmgap-slides-${port}`,
  // ローカルの画像（docs/captures/*.png）を file:// から読む。
  "--allow-file-access-from-files",
  "about:blank",
]);
chrome.stderr.on("data", () => {});

let wsUrl = null;
for (let i = 0; i < 60 && !wsUrl; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    wsUrl = list.find((t) => t.type === "page")?.webSocketDebuggerUrl ?? null;
  } catch {
    /* 起動待ち */
  }
  if (!wsUrl) await sleep(250);
}
if (!wsUrl) throw new Error("DevTools に繋がらない");

const ws = new WebSocket(wsUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const waiting = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) {
    waiting.get(m.id)(m.result);
    waiting.delete(m.id);
  }
};
const send = (method, params = {}) =>
  new Promise((r) => {
    const n = ++id;
    waiting.set(n, r);
    ws.send(JSON.stringify({ id: n, method, params }));
  });

await send("Page.enable");
await send("Page.navigate", { url: `file://${SRC}` });
await sleep(3000);

// **画像が入っていないまま書き出すのを止める。** file:// の読み込みが
// 失敗しても PDF は正常に出てしまい、**枠だけのスライド**が提出物になる。
const probe = await send("Runtime.evaluate", {
  expression: `JSON.stringify({
    slides: document.querySelectorAll(".slide").length,
    imgs: document.querySelectorAll("img").length,
    broken: [...document.querySelectorAll("img")].filter(i => !i.naturalWidth).map(i => i.getAttribute("src")),
  })`,
  returnByValue: true,
});
const st = JSON.parse(probe?.result?.value ?? "{}");
if (st.broken?.length) throw new Error(`画像が読めていない: ${st.broken.join(", ")}`);

// 13.333in × 7.5in = 16:9。`preferCSSPageSize` で @page の指定を使う。
const pdf = await send("Page.printToPDF", {
  printBackground: true,
  preferCSSPageSize: true,
  marginTop: 0,
  marginBottom: 0,
  marginLeft: 0,
  marginRight: 0,
});
writeFileSync(OUT, Buffer.from(pdf.data, "base64"));

ws.close();
chrome.kill();

console.log(`✓ ${OUT}`);
console.log(`  スライド ${st.slides} 枚 / 画像 ${st.imgs} 枚（読めないもの 0）`);
console.log("\n**ページ数がスライド数と一致していることを目で確かめること。**");
console.log("@page と .slide の寸法がずれると、1 行だけこぼれた空白ページが増える。");
