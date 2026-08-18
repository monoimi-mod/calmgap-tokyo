/**
 * 操作動画（スライド 4・5 枚目、および提出フォーム 3-8）を撮る。
 *
 *   node tools/shoot_demo.mjs              2 本とも docs/demo/ へ
 *   node tools/shoot_demo.mjs evidence     1 本だけ
 *
 * **説明ではなく実演にするための道具である。** この作品の主張は
 * 「順位が出る → なぜその順位かが辿れる → 根拠の施設が地図で確かめられる」
 * ことで、**スライドに文章で書くと 3 行になるが、画面では 20 秒で済む。**
 *
 * ## 実物のサイトを操作する。再現ではない
 *
 * 撮るのは公開中の `SITE` で、**画面の中身をこのファイルに書かない。**
 * 押す場所は CSS セレクタで指すだけなので、UI が変われば撮り直しが落ちる
 * ——落ちてくれるほうが、古い画面の動画が提出物に残るより良い。
 *
 * ## カーソルは描いている
 *
 * ヘッドレスの画面にマウスポインタは写らない。**何も押していないのに
 * 画面が変わる動画**になると、実演として読めない。そこで
 * `Input.dispatchMouseEvent` を送る座標と**同じ座標**に印を描いている。
 * **座標は 1 つの出所（`click()`）から出る**ので、印と実際のクリックが
 * 食い違うことは無い——別々に持つと「押していない場所を押したように
 * 見える動画」を作れてしまう。
 *
 * ## `--virtual-time-budget` を使わない
 *
 * `tools/shoot_captures.mjs` と同じ理由（メッシュが 1 つも描かれない）。
 * 実時間で待つ。
 */
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
// **既定は公開中のサイト。** 撮るものと審査員が見るものを同じにするため。
// `CALMGAP_SITE` で差し替えられるのは、**回線が落ちて公開サイトから
// 撮れないときの逃げ道**である（`npm run dev` の localhost など）。
// 配信データは `web/public/data/` が同一なので同じ画面になるが、
// **使ったときは記録に残すこと。**
const SITE = process.env.CALMGAP_SITE || "https://calmgap-tokyo.hiroaki-kuwabara.workers.dev";
const OUT_DIR = resolve("docs/demo");
const W = 1600;
const H = 900;
const FPS = 24;

// 起動から撮り始めるまで。下絵のタイルが入りきらないと
// **メッシュだけが浮いた動画**になる（shoot_captures.mjs の注記と同じ）。
const BOOT_MS = Number(process.env.CALMGAP_SHOT_WAIT_MS || 20000);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ 台本
 *
 * **`docs/presentation.md` 4 節と 1 対 1 で対応させる。** 台本が
 * 動画の中身を説明しているので、片方だけ直すと説明と画面が食い違う。
 */
const SCENARIOS = {
  evidence: {
    out: "demo-evidence",
    title: "順位 → 根拠 → 地図の点 → 施設の詳細",
    // ポスター（PDF 版のスライドに出る静止画）に使うフレームの位置。
    // 0〜1 の割合で、**「一覧と地図の点が両方写っている」ところ**を採る。
    poster: 0.60,
    async run(a) {
      await a.hold(1600); // ① 初期画面
      await a.click("table.data-table.is-ranking tbody tr:nth-child(2)", "② 提言リスト 2 位を開く");
      await a.hold(2200); // 地図が寄り、根拠と内訳が出る
      await a.scrollTo('.factor[data-hl="welfare"]', "③ 需要側の内訳まで送る");
      await a.hold(1200);
      await a.click('.factor[data-hl="welfare"]', "④ 障害福祉サービス事業所の行を押す");
      await a.hold(2600); // 徒歩圏 800m の円と 58 点が出る
      await a.scrollTo(".fl-item[data-i]", "⑤ 一覧まで送る");
      await a.hold(1200);
      await a.click(".fl-item[data-i]", "⑥ 一覧の 1 件を押す");
      await a.hold(3200); // 地図の吹き出しに詳細が出る
    },
  },
  unreachable: {
    out: "demo-unreachable",
    title: "絞り込むと、斜線の 148 区画が浮かび上がる",
    poster: 0.9,
    async run(a) {
      await a.hold(2400); // 初期画面（東京全体の優先度）
      await a.clickText(".finding-btn", "絞り込んで地図に出す", "① 絞り込んで地図に出す");
      await a.hold(5200); // 上位が消え、斜線の区画が現れる
    },
  },
  weights: {
    out: "demo-weights",
    title: "重みを変えると順位も変わる",
    poster: 0.86,
    async run(a) {
      await a.hold(1200);
      // **プリセットは左パネルの折り返しの下にある。** 先に出しておかないと
      // 「押した瞬間」が画面に写らず、順位表だけが理由なく入れ替わる。
      await a.scrollTo("#presets", "① 重みのプリセットまで送る");
      await a.hold(900);
      await a.clickText(".preset-btn", "通所先・通学先の近く", "② 通所・通学を重視");
      await a.hold(3000); // 順位表が入れ替わる
      // **プリセットで終わらせない。** つまみを自分で動かせることまで見せる。
      await a.scrollTo("#sliders", "③ スライダーまで送る");
      await a.hold(900);
      await a.dragSlider("#w-station_flow", 1.0, "④ 駅乗降規模のつまみを上げる");
      await a.hold(3400);
    },
  },
};

/* ------------------------------------------------------------------ CDP */

async function connect() {
  const port = 9822 + Math.floor(Math.random() * 400);
  const profile = `/tmp/calmgap-demo-${port}`;
  const chrome = spawn(CHROME, [
    "--headless=new",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--hide-scrollbars",
    `--window-size=${W},${H}`,
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
  // **落とし切ってから消す。** kill 直後に消すと Chrome がまだ書いていて
  // ENOTEMPTY で落ち、**収録が成功していても最後に例外で終わる。**
  const close = async () => {
    ws.close();
    chrome.kill();
    await sleep(900);
    rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  };
  return { send, close };
}

/** 画面に描くカーソル。**送るマウス座標と同じ値でしか動かさない。** */
const CURSOR_JS = `
(() => {
  const d = document.createElement("div");
  d.id = "__demo_cursor";
  d.style.cssText = [
    "position:fixed", "left:0", "top:0", "width:22px", "height:22px",
    "margin:-11px 0 0 -11px", "border-radius:50%",
    "background:rgba(44,122,140,.28)", "border:2px solid #2c7a8c",
    "box-shadow:0 0 0 1px rgba(255,255,255,.9)",
    "z-index:2147483647", "pointer-events:none",
    "transition:transform .09s linear", "opacity:0",
  ].join(";");
  document.body.appendChild(d);
  const ring = document.createElement("div");
  ring.id = "__demo_ring";
  ring.style.cssText = [
    "position:fixed", "left:0", "top:0", "width:22px", "height:22px",
    "margin:-11px 0 0 -11px", "border-radius:50%",
    "border:2px solid #c2542f", "z-index:2147483646", "pointer-events:none",
    "opacity:0",
  ].join(";");
  document.body.appendChild(ring);
  window.__demoMove = (x, y) => {
    d.style.opacity = "1";
    d.style.transform = "translate(" + x + "px," + y + "px)";
    ring.style.transform = "translate(" + x + "px," + y + "px)";
  };
  window.__demoTap = () => {
    ring.style.transition = "none";
    ring.style.opacity = "1";
    ring.animate(
      [
        { transform: ring.style.transform + " scale(1)", opacity: 1 },
        { transform: ring.style.transform + " scale(2.6)", opacity: 0 },
      ],
      { duration: 420, easing: "ease-out" },
    );
  };
})()`;

/** 撮る側。台本（SCENARIOS）はこの 4 つの動作しか使わない。 */
function actor(send, log) {
  let cx = W * 0.5;
  let cy = H * 0.5;

  const evalJs = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    if (r?.exceptionDetails) throw new Error(`ページ内で例外: ${r.exceptionDetails.text}`);
    return r?.result?.value;
  };

  /** セレクタの中心座標。**見つからなければ止める**（黙って撮り続けない）。
   *
   * **画面の外に在る要素も止める。** `getBoundingClientRect` は折り返しの
   * 下にある要素の座標も返すので、そのまま送ると
   * **見えていない要素の座標を押したことにして、実際には別のものを押す。**
   * 一度これで撮った——プリセットのボタンが左パネルの下にあり、
   * 順位表が既定のままの動画が「重みを変えた動画」として出来上がった。
   * **画面を見ても、変わらなかっただけに見えて誤りだと分からない。**
   */
  const centerOf = async (sel, note) => {
    const box = await evalJs(`(() => {
      const e = document.querySelector(${JSON.stringify(sel)});
      if (!e) return null;
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return JSON.stringify({
        x: r.left + r.width / 2,
        y: r.top + Math.min(r.height / 2, 40),
        inView: r.top >= 0 && r.bottom <= ${H} && r.left >= 0 && r.right <= ${W},
      });
    })()`);
    if (!box) throw new Error(`${note}: ${sel} が見つからない（UI が変わった可能性）`);
    const p = JSON.parse(box);
    if (!p.inView) throw new Error(`${note}: ${sel} が画面の外にある。先に scrollTo すること`);
    return p;
  };

  /** 人の速さでカーソルを運ぶ。**運んだ先でしかクリックしない。** */
  const moveTo = async (x, y) => {
    const steps = 14;
    const x0 = cx;
    const y0 = cy;
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const e = t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t); // ease-in-out
      cx = x0 + (x - x0) * e;
      cy = y0 + (y - y0) * e;
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: cx, y: cy, buttons: 0 });
      await evalJs(`window.__demoMove(${cx.toFixed(1)}, ${cy.toFixed(1)})`);
      await sleep(22);
    }
  };

  const tap = async () => {
    await evalJs("window.__demoTap()");
    await send("Input.dispatchMouseEvent", {
      type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1, buttons: 1,
    });
    await sleep(90);
    await send("Input.dispatchMouseEvent", {
      type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1, buttons: 0,
    });
  };

  return {
    hold: (ms) => sleep(ms),

    async click(sel, note) {
      const { x, y } = await centerOf(sel, note);
      log(note);
      await moveTo(x, y);
      await sleep(200);
      await tap();
    },

    /** 文字で押す。プリセットのボタンは順序でなく**表示名**で指す。 */
    async clickText(sel, text, note) {
      const found = await evalJs(`(() => {
        const e = [...document.querySelectorAll(${JSON.stringify(sel)})]
          .find(b => b.textContent.trim() === ${JSON.stringify(text)});
        if (!e) return null;
        e.dataset.demoTarget = "1";
        return "1";
      })()`);
      if (!found) throw new Error(`${note}: 「${text}」というボタンが無い`);
      await this.click(`${sel}[data-demo-target]`, note);
      await evalJs(`document.querySelectorAll("[data-demo-target]").forEach(e => delete e.dataset.demoTarget)`);
    },

    /** スライダーをつまんで動かす。**プリセットを押すだけの動画にしない**
     * ——「用意された 4 パターンから選ぶ機能」に見えてしまう。
     * 利用者が**自分で重視するものを調整できる**ことは、つまみが動いて
     * 順位表が入れ替わるところを見せるのがいちばん短い。
     *
     * `to` は 0〜1（min〜max のどこへ動かすか）。**値ではなく割合で指す**
     * ——スライダーの上限は `config.py` の側で決まるので、
     * ここに `2.0` と書くと片方だけ古くなる。
     */
    async dragSlider(sel, to, note) {
      const box = await evalJs(`(() => {
        const e = document.querySelector(${JSON.stringify(sel)});
        if (!e) return null;
        const r = e.getBoundingClientRect();
        if (r.top < 0 || r.bottom > ${H}) return "offscreen";
        const min = Number(e.min), max = Number(e.max), v = Number(e.value);
        return JSON.stringify({
          y: r.top + r.height / 2,
          from: r.left + (r.width * (v - min)) / (max - min),
          left: r.left, width: r.width,
        });
      })()`);
      if (!box) throw new Error(`${note}: ${sel} が見つからない`);
      if (box === "offscreen") throw new Error(`${note}: ${sel} が画面の外にある。先に scrollTo すること`);
      const { y, left, width, from } = JSON.parse(box);
      const target = left + width * to;

      log(note);
      await moveTo(from, y);
      await sleep(240);
      await evalJs("window.__demoTap()");
      await send("Input.dispatchMouseEvent", {
        type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1, buttons: 1,
      });
      // つまんだまま運ぶ。**押しっぱなしのまま動かす**ので moveTo は使えない
      // （あちらは buttons: 0 で送る）。
      //
      // **移動の側にも `button: "left"` が要る。** `buttons: 1` だけだと
      // Chrome の range 入力がつまみを追わず、**押した瞬間の値のまま止まる**
      // ——実測で 0.9 → 0.3（押した位置）になり、そこから動かなかった。
      // **動画は最後まで撮れて成功と表示される**ので、
      // 「重みを変えた動画」として使えないものが出来上がる。
      const steps = 22;
      const x0 = cx;
      for (let i = 1; i <= steps; i++) {
        cx = x0 + (target - x0) * (i / steps);
        await send("Input.dispatchMouseEvent", {
          type: "mouseMoved", x: cx, y: cy, button: "left", buttons: 1,
        });
        await evalJs(`window.__demoMove(${cx.toFixed(1)}, ${cy.toFixed(1)})`);
        await sleep(45);
      }
      await send("Input.dispatchMouseEvent", {
        type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1, buttons: 0,
      });

      // **動いたことを確かめる。** つまみが動かなくても収録は最後まで走り、
      // 「重みを変えた動画」として使えないものが成功と表示される。
      const after = await evalJs(`(() => {
        const e = document.querySelector(${JSON.stringify(sel)});
        const min = Number(e.min), max = Number(e.max);
        return (Number(e.value) - min) / (max - min);
      })()`);
      if (Math.abs(after - to) > 0.06) {
        throw new Error(
          `${note}: つまみが動いていない（${sel} は ${(after * 100).toFixed(0)}% の位置で、` +
            `指定した ${(to * 100).toFixed(0)}% と違う）`,
        );
      }
    },

    /** パネルを送る。**スクロールも人の速さで**（一瞬で飛ぶと何が起きたか読めない）。 */
    async scrollTo(sel, note) {
      log(note);
      const ok = await evalJs(`(() => {
        const e = document.querySelector(${JSON.stringify(sel)});
        if (!e) return null;
        e.scrollIntoView({ behavior: "smooth", block: "center" });
        return "1";
      })()`);
      if (!ok) throw new Error(`${note}: ${sel} が見つからない`);
      await sleep(1100);
    },
  };
}

/* ------------------------------------------------------------------ 本体 */

async function record(key) {
  const sc = SCENARIOS[key];
  const tmp = `/tmp/calmgap-demo-frames-${key}-${Date.now()}`;
  mkdirSync(tmp, { recursive: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const { send, close } = await connect();
  try {
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Emulation.setDeviceMetricsOverride", {
      width: W, height: H, deviceScaleFactor: 1, mobile: false,
    });
    await send("Page.navigate", { url: SITE });
    await sleep(BOOT_MS);

    // **読み込み中の画面を撮り始めない。** shoot_captures.mjs と同じ検査。
    const probe = await send("Runtime.evaluate", {
      expression: `JSON.stringify({
        boot: !!document.getElementById("boot-overlay"),
        rows: document.querySelectorAll("table.data-table.is-ranking tbody tr").length,
      })`,
      returnByValue: true,
    });
    const state = JSON.parse(probe?.result?.value ?? "{}");
    if (state.boot) throw new Error("読み込み中の画面のまま撮り始めようとした");
    if (!state.rows) throw new Error("順位表が空のまま撮り始めようとした");

    await send("Runtime.evaluate", { expression: CURSOR_JS });

    /* --- 収録 ---
     *
     * **`Page.startScreencast` を使っていない。** screencast は
     * **画面が変わったときだけ**フレームを寄越すので、
     * 「押した結果を 3 秒見せる」区間がフレーム 1 枚に潰れる
     * （実測 10 秒の台本が 1.8 秒・25 枚になった）。
     * `Page.captureScreenshot` を回して**実時間で撮る**——この環境で
     * 約 19 fps 出るので、操作の速さをそのまま写せる。
     */
    let shooting = true;
    const frames = [];
    const loop = (async () => {
      while (shooting) {
        const shot = await send("Page.captureScreenshot", { format: "jpeg", quality: 90 });
        if (!shot?.data) continue;
        const name = `f${String(frames.length).padStart(5, "0")}.jpg`;
        writeFileSync(`${tmp}/${name}`, Buffer.from(shot.data, "base64"));
        frames.push({ name, t: Date.now() });
        await sleep(8); // 操作側のコマンドに順番を譲る
      }
    })();

    const steps = [];
    await sc.run(actor(send, (note) => steps.push(note)));
    await sleep(500);
    shooting = false;
    await loop;

    if (frames.length < 40) throw new Error(`フレームが ${frames.length} 枚しか取れていない`);

    /* --- ffmpeg へ渡す --- */
    const list = [];
    frames.forEach((f, i) => {
      const dur = i + 1 < frames.length ? (frames[i + 1].t - f.t) / 1000 : 0.6;
      list.push(`file '${tmp}/${f.name}'`, `duration ${Math.max(dur, 0.001).toFixed(3)}`);
    });
    // concat demuxer は最後の duration を無視するので、末尾の 1 枚を書き直す。
    list.push(`file '${tmp}/${frames[frames.length - 1].name}'`);
    writeFileSync(`${tmp}/list.txt`, list.join("\n"));

    const mp4 = `${OUT_DIR}/${sc.out}.mp4`;
    await run("ffmpeg", [
      "-y", "-f", "concat", "-safe", "0", "-i", `${tmp}/list.txt`,
      "-vf", `fps=${FPS},scale=${W}:${H}:flags=lanczos`,
      "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
      "-crf", "23", "-preset", "slow", "-movflags", "+faststart",
      mp4,
    ]);

    // **ポスター（PDF 版のスライドに出る静止画）も同じ収録から採る。**
    // 別撮りにすると、動画と静止画が違う状態を写し得る。
    const pi = Math.min(frames.length - 1, Math.round((frames.length - 1) * sc.poster));
    const poster = `${OUT_DIR}/${sc.out}-poster.png`;
    await run("ffmpeg", [
      "-y", "-i", `${tmp}/${frames[pi].name}`, poster,
    ]);

    const dur = (frames[frames.length - 1].t - frames[0].t) / 1000;
    console.log(`✓ ${mp4}`);
    console.log(`  ${sc.title} / ${dur.toFixed(1)} 秒 / フレーム ${frames.length} 枚`);
    for (const s of steps) console.log(`    ${s}`);
    console.log(`  ポスター ${poster}`);
    return { key, mp4, poster, dur, steps };
  } finally {
    await close();
    rmSync(tmp, { recursive: true, force: true });
  }
}

function run(cmd, args) {
  return new Promise((res, rej) => {
    const p = spawn(cmd, args);
    let err = "";
    p.stderr.on("data", (d) => (err += d));
    p.on("error", rej);
    p.on("close", (code) =>
      code === 0 ? res() : rej(new Error(`${cmd} が失敗した (${code})\n${err.slice(-1500)}`)),
    );
  });
}

const only = process.argv[2];
const keys = only ? [only] : Object.keys(SCENARIOS);
for (const k of keys) {
  if (!SCENARIOS[k]) throw new Error(`知らない台本: ${k}（${Object.keys(SCENARIOS).join(" / ")}）`);
  await record(k);
}

if (!existsSync(`${OUT_DIR}/demo-evidence.mp4`)) process.exitCode = 1;

console.log("\n**必ず動画を目で見ること。** 自動で確かめられるのは");
console.log("「読み込み中でない・順位表が空でない・押す先が在る」までで、");
console.log("**地図にメッシュと点が描かれているかは検査できていない**");
console.log("（tools/shoot_captures.mjs の注記と同じ理由）。");
