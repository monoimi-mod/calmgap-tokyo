/**
 * 「徒歩圏の件数」を、画面と同じやり方で数え直して配信値と突き合わせる。
 *
 *     node tools/facility_parity.mjs
 *
 * **なぜ要るか。** 画面は「徒歩圏に事業所 60 件」と書いた隣で、その 60 点を
 * 地図に光らせる。**表示される点の数と表の数字が一致しなければ、
 * ハイライトは説明ではなく矛盾になる。**
 *
 * ズレる経路は 1 つしかない——距離の測り方。Python は平面直角座標系
 * （EPSG:6677）で数え、ブラウザで緯度経度から測ると 800m の境界付近で
 * 1〜2 件変わる。そこで **Python が使っているのと同じ投影座標を配って**、
 * 両者に同じ引き算をさせている（`x` / `y` と、メッシュの `mx` / `my`）。
 *
 * この検査は「同じになるはず」を実際に確かめる。座標は cm 精度に丸めて
 * 配信しているので、**半径のちょうど境界に 1cm 以内で載る点があれば
 * ここで落ちる**（現在のデータには 1 件も無い）。
 *
 * parity_check.mjs がスコアについてやっていることの、実数版。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const DATA = join(ROOT, "web", "public", "data");

const load = (name) => JSON.parse(readFileSync(join(DATA, name), "utf8"));

const meta = load("meta.json");
const mesh = load("mesh.geojson");
const demand = load("demand_points.geojson");
const hosts = load("hosts.geojson");
const noise = load("noise_points.geojson");

/** 半径内の点を数える。ブラウザ側とまったく同じ式であること。 */
function countWithin(cx, cy, points, radius) {
  const r2 = radius * radius;
  let n = 0;
  for (const p of points) {
    const dx = p.x - cx;
    const dy = p.y - cy;
    if (dx * dx + dy * dy <= r2) n++;
  }
  return n;
}

const xy = (fc, filter) =>
  fc.features
    .filter((f) => filter(f.properties))
    .map((f) => ({ x: f.properties.x, y: f.properties.y }));

const radius = meta.fact_radius_m;
const groups = [
  {
    field: "f_welfare_n",
    label: "徒歩圏の障害福祉サービス事業所",
    points: xy(demand, (p) => p.layer === "welfare"),
    radius: radius.welfare,
  },
  {
    field: "f_school_n",
    label: "徒歩圏の特別支援学校",
    points: xy(demand, (p) => p.layer === "school"),
    radius: radius.school,
  },
  {
    field: "f_clinic_n",
    label: "徒歩圏の精神科・心療内科",
    points: xy(demand, (p) => p.layer === "clinic"),
    radius: radius.clinic,
  },
  // **駅を後から足した。** 以前は「1,500m 以内の最寄り 1 駅」しか出しておらず
  // 件数が無かったので、この検査の対象外だった。件数を出すようになった以上、
  // 他の層と同じく「表の数字 = 地図の点 = 一覧の行数」を確かめる。
  {
    field: "f_station_n",
    label: "徒歩圏の駅",
    points: xy(demand, (p) => p.layer === "station"),
    radius: radius.station,
  },
  {
    field: "f_host_n",
    label: "徒歩圏の区の公共施設",
    points: xy(hosts, () => true),
    radius: radius.host,
  },
  // **騒音の測定点は「徒歩圏」ではない。** 半径は IDW の打ち切り距離で、
  // 「この区画の騒音値を作るのに使われた点」を意味する。
  // 0 件なら、その区画の値は測定ではなく 23 区の中央値である——
  // 画面はそう書くので、件数と光る点はここでも一致していなければならない。
  {
    field: "f_noise_n",
    label: "内挿に使った騒音測定点",
    points: xy(noise, () => true),
    radius: radius.noise,
  },
];

console.log("\n徒歩圏の件数の照合（Python の配信値 ↔ 画面と同じ数え方）\n");

let failed = 0;
for (const g of groups) {
  let worst = 0;
  let worstCode = "";
  let mismatched = 0;

  for (const f of mesh.features) {
    const p = f.properties;
    if (typeof p.mx !== "number") {
      console.error(`  mx/my が無い区画がある: ${p.c}`);
      failed++;
      break;
    }
    const got = countWithin(p.mx, p.my, g.points, g.radius);
    const want = p[g.field] ?? 0;
    const d = Math.abs(got - want);
    if (d > 0) {
      mismatched++;
      if (d > worst) {
        worst = d;
        worstCode = p.c;
      }
    }
  }

  const ok = mismatched === 0;
  if (!ok) failed++;
  console.log(
    `  ${ok ? "✓" : "✗"} ${g.label.padEnd(22)} 半径 ${String(g.radius).padStart(5)}m ` +
      `点 ${String(g.points.length).padStart(6)} 件` +
      (ok
        ? // 件数は数え直したものを出す。**ここに 9,507 と書いてあったので、
          // 模擬モード（3,009 区画）でも「全 9,507 区画で一致」と表示していた。**
          `  全 ${mesh.features.length.toLocaleString("en-US")} 区画で一致`
        : `  ${mismatched} 区画で不一致（最大 ${worst} 件・例 ${worstCode}）`),
  );
}

if (failed) {
  console.log(
    "\n不一致がある。**この状態で施設ハイライトを出してはいけない**——\n" +
      "「60 件」と書いた隣で 59 点しか光らないことになる。\n" +
      "座標の丸め精度（build.py の _write_geojson）か、半径の出所を疑うこと。\n",
  );
  process.exit(1);
}
console.log("\n  ✓ 画面が光らせる点の数は、表の数字と一致する\n");
