/**
 * 提出用スライド（5-1）を `docs/slides.html` から PPTX へ書き出す。
 *
 *   node tools/slides_pptx.mjs       → docs/slides.pptx
 *
 * ## なぜ PPTX も作るのか
 *
 * **PDF は動画を再生できない。** この作品の強さは
 * 「順位が出る → 根拠が辿れる → 地図の点で確かめられる」という**操作**にあり、
 * 静止画と文章で説明すると 3 行になるものが、動くと 20 秒で伝わる。
 * 提出フォーム 5-1 は PowerPoint / PDF のどちらでも可なので、
 * **動画が再生される側**を主の提出物にする。
 *
 * ## フォントが崩れないのは、文字を 1 つも入れていないから
 *
 * PPTX を避けてきた理由は「日本語のフォントが相手の環境に無いと
 * 行が折り返してレイアウトごと崩れ、しかも作った側の画面では
 * 正しく見えるので気付けない」ことだった（`slides_pdf.mjs`）。
 * **この書き出しはその条件を外す**——各ページは HTML を焼いた画像 1 枚で、
 * PPTX 側にテキストボックスが 1 つも無い。置き換わるフォントが存在しない。
 *
 * 代わりに失うのは**文字の検索と選択**である。だから
 * **PDF も併せて書き出しておく**（`node tools/slides_pdf.mjs`）。
 *
 * ## 動画の位置は HTML が決める
 *
 * `docs/slides.html` の `[data-video]` が付いた要素の矩形を読み、
 * そこへ mp4 を重ねる。**座標をこのファイルに書かない**——
 * 2 か所に持つと、スライドのレイアウトを直したときに片方だけ古くなる。
 * その要素の `src`（ポスター画像）がそのまま PowerPoint の表紙になるので、
 * **PDF に写っているコマと、PPTX の再生前に見えるコマが同じもの**になる。
 */
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync, rmSync, existsSync, readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import PptxGenJS from "pptxgenjs";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SRC = resolve("docs/slides.html");
const OUT = resolve("docs/slides.pptx");
const TMP = "/tmp/calmgap-slides-png";

// 13.333in × 7.5in（16:9）。**CSS の @page と同じ値**であること。
const IN_W = 13.333;
const IN_H = 7.5;
// CSS ピクセルから インチへ。ブラウザの 1in は常に 96px。
const PX_PER_IN = 96;
// 焼く解像度。2 倍で 2560×1440。
const SCALE = 2;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const port = 9622 + Math.floor(Math.random() * 400);
const profile = `/tmp/calmgap-pptx-${port}`;
const chrome = spawn(CHROME, [
  "--headless=new",
  "--hide-scrollbars",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  // docs/captures/*.png と docs/demo/*.png を file:// から読む。
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
await send("Emulation.setDeviceMetricsOverride", {
  width: Math.round(IN_W * PX_PER_IN),
  height: Math.round(IN_H * PX_PER_IN),
  deviceScaleFactor: 1,
  mobile: false,
});
await send("Page.navigate", { url: `file://${SRC}` });
await sleep(3000);

// **画像が入っていないまま書き出すのを止める。** file:// の読み込みが
// 失敗しても書き出しは成功し、**枠だけのスライド**が提出物になる。
const probe = await send("Runtime.evaluate", {
  expression: `JSON.stringify({
    broken: [...document.querySelectorAll("img")].filter(i => !i.naturalWidth).map(i => i.getAttribute("src")),
    slides: [...document.querySelectorAll(".slide")].map((s, i) => {
      const r = s.getBoundingClientRect();
      const videos = [...s.querySelectorAll("[data-video]")].map(v => {
        const b = v.getBoundingClientRect();
        return {
          file: v.dataset.video,
          poster: v.getAttribute("src"),
          x: b.left - r.left, y: b.top - r.top, w: b.width, h: b.height,
        };
      });
      return { i, x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height, videos };
    }),
  })`,
  returnByValue: true,
});
const st = JSON.parse(probe?.result?.value ?? "{}");
if (st.broken?.length) throw new Error(`画像が読めていない: ${st.broken.join(", ")}`);
if (!st.slides?.length) throw new Error("スライドが 1 枚も無い");

// **1 ページの寸法が CSS と食い違っていたら止める。** ここがずれたまま
// 書き出すと、画像が引き伸ばされた PPTX が黙って出来上がる。
for (const s of st.slides) {
  const dw = Math.abs(s.w / PX_PER_IN - IN_W);
  const dh = Math.abs(s.h / PX_PER_IN - IN_H);
  if (dw > 0.02 || dh > 0.02) {
    throw new Error(
      `${s.i + 1} 枚目の寸法が ${(s.w / PX_PER_IN).toFixed(3)}in × ${(s.h / PX_PER_IN).toFixed(3)}in で、` +
        `${IN_W}in × ${IN_H}in と違う（CSS の @page と .slide を揃えること）`,
    );
  }
}

rmSync(TMP, { recursive: true, force: true });
mkdirSync(TMP, { recursive: true });

for (const s of st.slides) {
  const shot = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
    clip: { x: s.x, y: s.y, width: s.w, height: s.h, scale: SCALE },
  });
  s.png = `${TMP}/slide-${String(s.i + 1).padStart(2, "0")}.png`;
  writeFileSync(s.png, Buffer.from(shot.data, "base64"));
}

ws.close();
chrome.kill();
await sleep(700);
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });

/* ---------------------------------------------------------------- 組み立て */

const pptx = new PptxGenJS();
pptx.defineLayout({ name: "CALMGAP16x9", width: IN_W, height: IN_H });
pptx.layout = "CALMGAP16x9";
pptx.title = "カームダウン・クールダウンスペース 設置優先度マップ（calmgap-tokyo）";
pptx.author = "monoimi";

const b64 = (p) => `data:image/png;base64,${readFileSync(p).toString("base64")}`;

let embedded = 0;
for (const s of st.slides) {
  const slide = pptx.addSlide();
  slide.addImage({ data: b64(s.png), x: 0, y: 0, w: IN_W, h: IN_H });

  for (const v of s.videos) {
    const mp4 = resolve(dirname(SRC), "demo", v.file);
    if (!existsSync(mp4)) {
      throw new Error(`${v.file} が無い。先に \`node tools/shoot_demo.mjs\` を実行すること`);
    }
    slide.addMedia({
      type: "video",
      path: mp4,
      // **表紙は HTML が指しているポスター画像そのもの。**
      // 別の画像を選ぶと、PDF に写っているコマと食い違う。
      cover: b64(resolve(dirname(SRC), v.poster)),
      x: v.x / PX_PER_IN,
      y: v.y / PX_PER_IN,
      w: v.w / PX_PER_IN,
      h: v.h / PX_PER_IN,
    });
    embedded++;
  }
}

await pptx.writeFile({ fileName: OUT });
rmSync(TMP, { recursive: true, force: true });

const mb = (readFileSync(OUT).length / 1024 / 1024).toFixed(1);
console.log(`✓ ${OUT}`);
console.log(`  ${st.slides.length} 枚 / 動画 ${embedded} 本 / ${mb} MB（5-1 の上限は 100MB）`);
console.log("\n**必ず PowerPoint で開いて、動画が再生されることを目で確かめること。**");
console.log("この書き出しが自動で確かめられるのは「画像が読めている・寸法が 16:9・");
console.log("mp4 が存在する」までで、**再生できるかは検査していない。**");
