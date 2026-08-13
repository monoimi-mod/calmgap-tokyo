/**
 * 提出用キャプチャ（5-2・1600×900）を撮る。
 *
 *   node tools/shoot_captures.mjs                    3 枚を docs/captures/ へ
 *   node tools/shoot_captures.mjs <URL> <出力先>       1 枚だけ
 *
 * **`--virtual-time-budget` を使わない。** 仮想時間で早送りすると
 * `requestAnimationFrame` の発火とメッシュのアップロードが噛み合わず、
 * **9,507 区画が 1 つも描かれていない画像**が出る（`#f=unreachable` で
 * 実際に踏んだ。ベースマップだけが写るので、縮小表示では
 * 「そういう画面」に見えてしまう）。しかも**毎回同じバイト列になる**ので、
 * 撮り直しても同じものが出てきて時間切れまで気付けない。
 * 実時間で待ち、**撮る前に描いた数を地図へ問い合わせる**。
 *
 * 撮る状態は URL のハッシュだけで決まる（`web/src/main.ts` の `applyUrl`）。
 * **キャプチャの構図をコードに書かない**——URL が唯一の出所で、
 * 同じ URL を人がブラウザで開いても同じ画面になる。
 */
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { dirname } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SITE = "https://calmgap-tokyo.hiroaki-kuwabara.workers.dev";

/** docs/presentation.md の「キャプチャ 3 枚」と同じ順・同じ URL。 */
const SHOTS = [
  ["", "docs/captures/01-overview.png", "結論 2 枚と全体地図"],
  ["#c=5339463631", "docs/captures/02-evidence.png", "1 区画の根拠（1 位・江東区 亀戸）"],
  ["#f=unreachable", "docs/captures/03-unreachable.png", "徒歩圏に区の公共施設が無い区画だけ"],
];

// **下絵のタイルが入りきらないことがある。** 12 秒では地理院タイルが
// 半分しか届かず、メッシュだけが浮いた画像が出た（2026-08-07）。
// 撮り直すと直ることがあるので**時間で殴る**しかない。
// CALMGAP_SHOT_WAIT_MS で伸ばせる。
const WAIT_MS = Number(process.env.CALMGAP_SHOT_WAIT_MS || 20000);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shoot(url, out) {
  const port = 9222 + Math.floor(Math.random() * 500);
  const profile = `/tmp/calmgap-shoot-${port}`;
  const chrome = spawn(CHROME, [
    "--headless=new",
    // WebGL が無いと地図は描かれない（画面はそれ用の案内を出す）。
    // ヘッドレスでは SwiftShader で software 実装を使う。
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--hide-scrollbars",
    "--window-size=1600,900",
    // 提出物は淡色で揃える。端末の設定で色が変わっては困る。
    "--blink-settings=preferredColorScheme=1",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
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
    width: 1600,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Page.navigate", { url });
  await sleep(WAIT_MS);

  // **撮る前に、データが入っていることだけは確かめる。**
  // 読み込み中の画面や空の順位表を撮ってしまうのを止める。
  //
  // **メッシュが描かれているかは、ここでは検査できない。** 地図ハンドルを
  // 外へ出していないので `queryRenderedFeatures` を呼べず、canvas は
  // `preserveDrawingBuffer: false` なので画素も読めない。
  // **検査できないことを「通った」にしない**ので、画像は必ず目で見る。
  const probe = await send("Runtime.evaluate", {
    expression: `JSON.stringify({
      boot: !!document.getElementById("boot-overlay"),
      rows: document.querySelectorAll("table.data-table tbody tr").length,
    })`,
    returnByValue: true,
  });
  const state = JSON.parse(probe?.result?.value ?? "{}");

  const shotData = await send("Page.captureScreenshot", { format: "png" });
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, Buffer.from(shotData.data, "base64"));

  ws.close();
  chrome.kill();
  // **前の実行の残骸が下絵を落とす。** 落ちた実行のヘッドレスが生きたまま
  // 溜まると、地理院タイルが半分しか届かず**メッシュだけが浮いた画像**が
  // 出た（25 秒待っても直らず、残骸を落としたら直った。2026-08-07）。
  // 検査では捕まらない——順位表は埋まっているので「成功」と出る。
  rmSync(profile, { recursive: true, force: true });

  if (state.boot) throw new Error(`${out}: 読み込み中の画面のまま撮れた`);
  if (!state.rows) throw new Error(`${out}: 順位表が空のまま撮れた`);
  return state;
}

const [, , argUrl, argOut] = process.argv;
const list = argUrl && argOut ? [[argUrl.replace(SITE, ""), argOut, "指定"]] : SHOTS;

for (const [hash, out, what] of list) {
  const url = hash.startsWith("http") ? hash : `${SITE}/${hash}`;
  const state = await shoot(url, out);
  console.log(`✓ ${out}  ${what}  順位表 ${state.rows} 行`);
}
console.log("\n**必ず画像を目で見ること。** 自動で確かめられるのは");
console.log("「読み込み中でない・順位表が空でない」までで、**地図にメッシュが");
console.log("描かれているかは検査できていない**（上の注記）。");
