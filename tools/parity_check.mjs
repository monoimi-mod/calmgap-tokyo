/**
 * Python (etl/score.py) と TypeScript (web/src/score.ts) の
 * スコア合成が一致することを検証する。
 *
 * 本プロジェクトは同じ数式を 2 言語で実装している。
 * ETL 側で「既定重みでのスコア」を書き出し、ブラウザ側は
 * スライダーの値でそれを計算し直す。両者がズレていたら、
 * 地図に最初に表示される色と、スライダーを既定値に戻したときの色が
 * 食い違うことになり、ツールの信頼性が根本から崩れる。
 *
 *   node tools/parity_check.mjs
 *
 * mesh.geojson に入っている demand/load/priority は Python が計算した値。
 * これを TS の compose() が既定重みで再現できるかを突き合わせる。
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DATA = join(ROOT, "web", "public", "data");
const TOLERANCE = 1e-9;

function fail(msg) {
  console.error(`\n  ✗ ${msg}\n`);
  process.exit(1);
}

// --- score.ts を素の JS へ変換する（vite に同梱の esbuild を使う） ---
const tmp = mkdtempSync(join(tmpdir(), "calmgap-parity-"));
const bundle = join(tmp, "score.mjs");
try {
  execFileSync(
    join(ROOT, "web", "node_modules", ".bin", "esbuild"),
    [
      join(ROOT, "web", "src", "score.ts"),
      "--bundle",
      "--format=esm",
      `--outfile=${bundle}`,
      "--log-level=error",
    ],
    { stdio: "inherit" },
  );
} catch {
  fail("esbuild を実行できない。先に `cd web && npm install` を実行すること。");
}

const { compose } = await import(pathToFileURL(bundle).href);

// --- データ読み込み ---
let meta, mesh;
try {
  meta = JSON.parse(readFileSync(join(DATA, "meta.json"), "utf8"));
  mesh = JSON.parse(readFileSync(join(DATA, "mesh.geojson"), "utf8"));
} catch {
  fail("web/public/data が無い。先に `python -m etl.build` を実行すること。");
}

const rows = mesh.features.map((f) => f.properties);
const weights = Object.fromEntries(meta.components.map((c) => [c.key, c.weight]));

// --- 既定重みで再計算し、Python の出力と比較する ---
const got = compose(rows, meta.components, weights, meta.priority_alpha, meta.priority_beta);

let worst = { field: "", diff: 0, index: -1 };
for (const field of ["demand", "load", "priority"]) {
  for (let i = 0; i < rows.length; i++) {
    // Python 側は配信時に小数 4 桁へ丸めている。丸め幅の半分までは許容する。
    const diff = Math.abs(rows[i][field] - Math.round(got[field][i] * 1e4) / 1e4);
    if (diff > worst.diff) worst = { field, diff, index: i };
  }
}

console.log(`\n  メッシュ数        ${rows.length.toLocaleString("ja-JP")}`);
console.log(`  最大乖離          ${worst.diff.toExponential(3)} (${worst.field})`);

if (worst.diff > TOLERANCE) {
  console.error(
    `  該当メッシュ      ${rows[worst.index]?.c}\n` +
      `  Python            ${rows[worst.index]?.[worst.field]}\n` +
      `  TypeScript        ${got[worst.field][worst.index]}`,
  );
  rmSync(tmp, { recursive: true, force: true });
  fail("Python と TypeScript のスコアが一致しない。両方の compose() を見直すこと。");
}

// --- 片側の重みを全て 0 にしても地図が消えないこと ---
const zeroLoad = { ...weights };
for (const c of meta.components) if (c.side === "load") zeroLoad[c.key] = 0;
const degenerate = compose(rows, meta.components, zeroLoad, 1, 1);
if (!degenerate.priority.some((v) => v > 0)) {
  rmSync(tmp, { recursive: true, force: true });
  fail("負荷側の重みを 0 にすると全メッシュの優先度が 0 になる（中立扱いが効いていない）。");
}

rmSync(tmp, { recursive: true, force: true });
console.log("\n  ✓ Python と TypeScript のスコアは一致している");
console.log("  ✓ 片側の重みを 0 にしても優先度は消えない\n");
