/**
 * パネル UI の描画。
 *
 * 順位表はサーバ側の proposals.json をそのまま出すのではなく、
 * 現在のスライダー重みから毎回組み直す。重みを動かしたのに
 * 順位が変わらなければ、このツールは嘘をついていることになる。
 * （proposals.json は既定重みでの静的な書き出しであり、資料添付用に残してある）
 */

import { clusterAdjacent, gridIndex } from "./area";
import { compose, topFactors, type ScoreResult } from "./score";
import type { ComponentDef, Meta, MeshProps, Sensitivity, Weights } from "./types";

export interface AppState {
  meta: Meta;
  rows: MeshProps[];
  weights: Weights;
  score: ScoreResult;
  selected: string | null;
  /**
   * **タブは 2 つ。** かつては「提言リスト（地区単位）」と「表で見る（区画単位）」に
   * 分かれていたが、提言の単位を区画に戻した時点で、両者は
   * 「同じ順位表を文章で見るか数値で見るか」の違いしか無くなった。
   * 根拠文も同じ narrate() の出力で、2 箇所に同じ文が出ていた。
   */
  tab: "ranking" | "selected";
  activePreset: string;
  /** 地図に塗る値。順位は常に優先度で決まる。 */
  displayMode: "priority" | "demand" | "load";
  /**
   * 順位表に出す件数。**選べるようにしてあるのは、打ち切りに根拠が
   * 無いことを隠さないため**（優先度は連続していて、どこにも切れ目が無い）。
   */
  rankingN: number;
}

// 優先度は 9,507 区画の中で 0.99〜0.17 と動く。2 桁だと上位 100 件が
// すべて 0.99 か 1.00 になり、差が無いように見えていた（実際には在る）。
const fmt = (n: number, d = 3) => n.toFixed(d);

/**
 * 「上位◯%」。**順位から出す。優先度の値から出してはいけない。**
 *
 * かつては `(1 - priority) * 100` だった。priority がパーセンタイル順位
 * だった頃は正しかったが、2026-08-03 に「需要 × 負荷」の生値へ変えたので
 * 成り立たなくなった（渋谷駅前は 792 位 = 上位 8% なのに、
 * 生値 0.7219 から出すと 28% になる）。etl/hosts.py の _narrative と対。
 */
const pctRank = (rank: number, total: number) =>
  Math.max(1, Math.round((rank / total) * 100));

/* --------------------------------------------------------------- 順位表 */

/** 順位表の 1 行。**単位は区画。** 地区（「○○周辺」）は作らない。 */
export interface RankRow {
  index: number;
  rank: number;
  meshCode: string;
  ward: string;
  station: string;
  priority: number;
  demand: number;
  load: number;
  hostCount: number;
  unreachable: boolean;
  /** 表示中の上位 N 区画のうち、この区画に接しているものの数（8 近傍）。 */
  adjacentN: number;
}

/**
 * 現在の重みでの上位 N 区画。**束ねない。**
 *
 * かつては上位 40 区画を格子隣接で束ね、「区名＋最寄り駅名＋周辺」という
 * 地区を提言の単位にしていた。束ね方（格子隣接）には根拠があったが、
 * **どこまでを束ねるかには無かった**——地区の広がりは母数 40 で決まり、
 * 40 のあたりに切れ目は無い。「大塚・北池袋周辺は 6 区画」の 6 は
 * 場所の性質ではなく 40 で切ったことの帰結で、**「○○周辺」という名前が、
 * 計算していない広がりを主張していた**（etl/hosts.py の build_ranking と対）。
 *
 * 隣接は単位ではなく記述的な事実として残す。母数（表示件数）を必ず添える。
 */
export function rankingRows(state: AppState): RankRow[] {
  const { rows, score, meta } = state;
  const top = score.order.slice(0, state.rankingN);
  const idx = top.map((i) => gridIndex(rows[i].c));

  return top.map((rowIdx, k) => {
    const row = rows[rowIdx];
    let adjacent = 0;
    for (let j = 0; j < idx.length; j++) {
      if (j === k) continue;
      if (
        Math.abs(idx[k][0] - idx[j][0]) <= 1 &&
        Math.abs(idx[k][1] - idx[j][1]) <= 1
      ) {
        adjacent++;
      }
    }
    return {
      index: rowIdx,
      rank: k + 1,
      meshCode: row.c,
      ward: typeof row.w === "number" ? (meta.target_wards[row.w] ?? "") : "",
      station: (row.f_station_name as string) ?? "",
      priority: score.priority[rowIdx],
      demand: score.demand[rowIdx],
      load: score.load[rowIdx],
      hostCount: (row.f_host_n as number) ?? 0,
      unreachable: !row.host,
      adjacentN: adjacent,
    };
  });
}

/**
 * その区画に連なる上位区画のコード（自分を含む）。地図の破線に使う。
 *
 * **地区ではない。** 表示中の上位 N のうち格子の上でつながっているもの、
 * というだけ。件数を変えれば範囲も変わる——それが見えることに意味がある。
 */
export function clusterOf(state: AppState, meshCode: string | null): string[] {
  if (!meshCode) return [];
  const codes = state.score.order
    .slice(0, state.rankingN)
    .map((i) => state.rows[i].c);
  if (!codes.includes(meshCode)) return [];
  const groups = clusterAdjacent(codes);
  const hit = groups.find((g) => g.some((m) => codes[m] === meshCode));
  return hit ? hit.map((m) => codes[m]) : [];
}

const num = (n: number) => n.toLocaleString("ja-JP");

export interface FactSection {
  title: string;
  /** その節の値が何を数えたものかを一度だけ言う。行ごとに繰り返さない。 */
  note: string;
  items: { label: string; value: string }[];
}

/**
 * その区画の「実数」。ETL が f_* として配信している表示専用の値。
 *
 * スコアは順位に正規化された相対値なので、それ単体では提言文にならない。
 * 「需要 0.94」ではなく「徒歩圏に事業所 4 件・定員 77 人」と書けて初めて
 * 予算会議の資料になる。
 *
 * **節に分けているのは、1 枚の表に性質の違う値が混ざっていたから。**
 * かつては 8 行を平らに並べており、半径 800m の件数・1,500m 以内の最寄り 1 件・
 * 区画自身の値・半径 700m の件数が同じ見た目で並んでいた。しかも「徒歩圏の」が
 * 付いているのは障害福祉サービス事業所だけで、同じ 800m の精神科・心療内科には
 * 付いていない。**半径が 1 つだけ違うこと（公共施設の 700m）が、
 * 節に分けて初めて見える。**
 */
export function factsOf(row: MeshProps, radius: Meta["fact_radius_m"]): FactSection[] {
  const g = (k: string) => row[k] as number | undefined;
  const s = (k: string) => row[k] as string | undefined;

  // 半径ごとに束ねる。3 層とも同じ 800m なら 1 節にまとまり、
  // 帯域を層ごとに変えたら節が自動的に分かれる。
  const walk: (FactSection["items"][number] & { r: number })[] = [];
  if (g("f_welfare_n")) {
    const cap = g("f_welfare_cap");
    // 徒歩圏は区界で切っていない（エッジ効果を避けるため入力を区界の外側
    // 2km まで拾う設計）。**その事実をカードに出す**——外周のメッシュは
    // 「隣の市の事業所」で需要が決まっていることがある（docs/issues.md B1）。
    const outside = g("f_welfare_outside_n") ?? 0;
    walk.push({
      label: "障害福祉サービス事業所",
      r: radius.welfare,
      value:
        `${num(g("f_welfare_n")!)}件${cap ? `（定員 ${num(cap)}人）` : ""}` +
        (outside ? ` ※うち対象23区の外 ${num(outside)}件` : ""),
    });
  }
  if (g("f_school_n")) {
    walk.push({
      label: "特別支援学校",
      r: radius.school,
      value: `${num(g("f_school_n")!)}校`,
    });
  }
  if (g("f_clinic_n")) {
    walk.push({
      label: "精神科・心療内科",
      r: radius.clinic,
      value: `${num(g("f_clinic_n")!)}件`,
    });
  }

  const nearest: FactSection["items"] = [];
  if (s("f_station_name")) {
    const r = g("f_station_riders");
    const d = g("f_station_dist");
    nearest.push({
      label: s("f_station_name")!,
      value:
        (r ? `乗降 ${num(r)}人/日` : "乗降規模不明") +
        (d != null ? ` / ${num(d)}m` : ""),
    });
  }

  const own: FactSection["items"] = [];
  if (s("f_zoning_name")) own.push({ label: "用途地域", value: s("f_zoning_name")! });
  if (g("f_noise_db")) {
    own.push({ label: "推定騒音", value: `${g("f_noise_db")} dB (LAeq)` });
  }
  own.push({
    label: "緑・公園被覆",
    value: g("f_green_pct") ? `${g("f_green_pct")}%` : "0%（屋外に退避先なし）",
  });

  // 供給側について言える唯一の実数。数えているのは施設一覧の行数であって
  // 建物の数ではない（同じ建物の別種別が別行で載っている。docs/issues.md）。
  const host: FactSection["items"] = [
    {
      label: "区の公共施設",
      value: g("f_host_n") ? `${num(g("f_host_n")!)}件（一覧の行数）` : "0件",
    },
  ];

  const walkSections: FactSection[] = [...new Set(walk.map((w) => w.r))]
    .sort((a, b) => a - b)
    .map((r) => ({
      title: `徒歩圏（半径 ${num(r)}m）にあるもの`,
      note:
        `この区画の中心から ${num(r)}m 以内にある件数です。` +
        "区界では切っていないため、隣の自治体の施設も含みます。",
      items: walk.filter((w) => w.r === r).map(({ label, value }) => ({ label, value })),
    }));

  return [
    ...walkSections,
    {
      title: "最寄り駅",
      note: `${num(radius.station_max)}m 以内で最も近い 1 駅です。件数ではありません。`,
      items: nearest,
    },
    {
      title: "この区画そのものの値",
      note: "周囲を数えたものではなく、区画自身に与えられた値です。",
      items: own,
    },
    {
      title: `既存の公共施設（半径 ${num(radius.host)}m）`,
      note:
        `到達可否の判定だけ半径が ${num(radius.host)}m で、上の徒歩圏とは別の距離です。` +
        "23 区が公開する公共施設一覧のみを数えており、都・国・民間の施設は入っていません。",
      items: host,
    },
  ].filter((sec) => sec.items.length > 0);
}

/**
 * 予算会議にそのまま出せる日本語の根拠文。etl/hosts.py の _narrative と対応。
 * 正規化スコアの言い換えではなく、実数を並べて根拠にする。
 */
function narrate(
  state: AppState,
  idx: number,
  rank: number,
  weights: Weights,
  meta: Meta,
): string {
  const { rows, score } = state;
  const row = rows[idx];
  const parts: string[] = [];

  // **母数を必ず書く。** 「需要 0.99 × 負荷 0.98」は 9,507 区画の中での
  // 順位でしかなく、対象の取り方で全部変わる（実際、模擬 → 2 区 → 23 区で
  // 上位 10 件は毎回入れ替わった）。文書には書いてあったが、
  // カードの文面そのものには母数が無かった（docs/issues.md B2）。
  parts.push(
    `優先度 第${rank}位 / ${meta.mesh_count.toLocaleString("ja-JP")}区画中` +
      `（上位${pctRank(rank, meta.mesh_count)}%）。`,
    `需要 ${fmt(score.demand[idx])} × 負荷 ${fmt(score.load[idx])} = ` +
      `${fmt(score.priority[idx])}` +
      "（需要と負荷は対象地域内での相対値で、絶対的な水準ではない）。",
  );

  // --- 需要側を実数で述べる ---
  const demandBits: string[] = [];
  const wn = row.f_welfare_n as number | undefined;
  const wc = row.f_welfare_cap as number | undefined;
  if (wn) {
    // 区外の件数は、多いときだけ書く。0 件や 1〜2 件で毎回添えると
    // 提言文が読みにくくなるだけで、判断は変わらない。
    // 2 割を超えると「この区画の需要は隣の自治体の施設で決まっている」と
    // 読むべき水準になる（docs/issues.md B1）。
    const outside = (row.f_welfare_outside_n as number) ?? 0;
    demandBits.push(
      `徒歩圏に障害福祉サービス事業所${num(wn)}件` +
        (wc ? `（定員計${num(wc)}人）` : "") +
        (outside / wn >= 0.2
          ? `——ただし${num(outside)}件は対象23区の外にあり、この区画の需要は隣接自治体の施設に依存している`
          : ""),
    );
  }
  if (row.f_school_n) demandBits.push(`特別支援学校${num(row.f_school_n as number)}校`);
  if (row.f_clinic_n) demandBits.push(`精神科・心療内科${num(row.f_clinic_n as number)}件`);
  const stName = row.f_station_name as string | undefined;
  const stRiders = row.f_station_riders as number | undefined;
  if (stName) {
    demandBits.push(
      `最寄りの${stName}は乗降${stRiders ? `${num(stRiders)}人/日` : "規模大"}`,
    );
  }
  if (demandBits.length) parts.push(`${demandBits.join("、")}。`);

  // --- 負荷側を実数で述べる ---
  const loadBits: string[] = [];
  if (row.f_zoning_name) loadBits.push(`用途地域は${row.f_zoning_name}`);
  if (row.f_noise_db) loadBits.push(`推定騒音${row.f_noise_db}dB`);
  const green = (row.f_green_pct as number) ?? 0;
  loadBits.push(green > 0 ? `緑・公園被覆${green}%` : "緑・公園被覆なし");
  parts.push(`${loadBits.join("、")}。`);

  if (green < 3) {
    parts.push("屋外に代替の退避先が存在しない。");
  }

  // 重みを大きく動かしたときに、何が効いているかを補足する。
  const top = topFactors(row, meta.components, "demand", weights, 1);
  if (top.length && top[0].normalized > 0.9) {
    parts.push(`現在の重みでは${shortLabel(top[0].component)}が需要側の最大要因。`);
  }

  // 供給側は「在るか / 幾つ在るか」までしか述べない。
  // かつてここに「設置候補: ◯◯図書館」と書いていたが、それはこのモデルが
  // 計算していない結論だった（施設の適性を測る構成要素が一つも無い）。
  const host = (row.host as string) ?? "";
  if (host) {
    const dist = row.host_d as number | undefined;
    const near = dist != null ? `、最寄りは${host}で約${num(dist)}m` : `（例: ${host}）`;
    parts.push(
      `徒歩圏（${meta.host_max_distance_m}m）に区の公共施設が` +
        `${num((row.f_host_n as number) ?? 0)}件${near}` +
        "（施設側の余剰空間も運営体制も測っておらず、適否の判断は含まない）。",
    );
  } else {
    parts.push(
      `半径${meta.host_max_distance_m}m 以内に区の公共施設が 1 件も無い。` +
        "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要る。",
    );
  }
  return parts.join("");
}

const shortLabel = (c: ComponentDef) => c.label.split("（")[0];

/* ------------------------------------------------------------------ 見出し数値 */

/**
 * 見出しの 1 数値。
 *
 * **順位ではなく到達不可を出す。** 順位は特別支援学校というレイヤー 1 枚に
 * 強く依存していて（外すと上位 10 件が全部入れ替わる。docs/issues.md A4）、
 * 見出しに据えられるほど頑健ではない。一方この数字は
 * **重みもスコアも帯域も通っておらず**、最寄りの公共施設までの距離だけで決まる。
 * スライダーをどう動かしても変わらない、というのがそのまま長所になる。
 *
 * かつては「優先度上位 50 区画のうち到達不可」を見出しにしていたが、
 * 供給側を 23 区分そろえた結果この値は 0 になり、見出しが空振りしていた。
 * その数字は現在の重みで動くので、副次の行として残す。
 */
export function renderStat(state: AppState): void {
  const { rows, score, meta } = state;
  // 順位表に出している件数と揃える。ここだけ 50 固定だと、
  // 表示件数を変えたときに 2 つの数字が食い違う。
  const N = Math.min(state.rankingN, rows.length);
  const top = score.order.slice(0, N);
  const uncoveredTop = top.filter((i) => !rows[i].host).length;

  const kickerEl = document.getElementById("stat-kicker")!;
  const valueEl = document.getElementById("stat-value")!;
  const labelEl = document.getElementById("stat-label")!;
  const u = meta.unreachable;

  // **「何の数字か」を数字より先に置く。** 以前はここが無く、いきなり
  // 「1,058 区画 / 11.1%」だった。区画の定義も「到達不可」の定義も
  // 画面に無いまま大きい数だけが出るので、大きいことしか伝わらない。
  kickerEl.textContent =
    `徒歩圏（半径 ${num(meta.host_max_distance_m)}m）に区の公共施設が 1 件も無い区画`;

  // 母数を必ず添える。提言文には母数を書く方針にしていたのに、
  // 画面でいちばん大きいこの数字にだけ母数が無かった。
  valueEl.innerHTML =
    `${u.count.toLocaleString("ja-JP")}<small> / ${meta.mesh_count.toLocaleString("ja-JP")} 区画` +
    `（${Math.round(u.ratio * 1000) / 10}%）</small>`;

  labelEl.innerHTML =
    "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要る区画です。" +
    "<b>この数字は重みにもスコアにも依存しません</b>（スライダーを動かしても変わりません）。" +
    `<br><br>うち<b>区内で優先度が中位以上</b>のものが ${u.mid_or_above} 区画。` +
    "<b>区ごとにこの割合を出して並べてはいけません</b>——皇居・羽田空港・埋立地・" +
    "河川敷を含む区で高く出るだけで、非市街地の面積比でほぼ決まります。" +
    "区をまたいで比べるならこちらを使ってください。" +
    `<br><br>現在の重みでの上位 ${N} 区画のうち、公共施設が徒歩圏に無いものは ${uncoveredTop} 件。`;
}

/**
 * 「これは何か」を画面の最初に置く。
 *
 * 都のオープンデータカタログの可視化事例はどれも「説明 1 文 → 使用データセット」
 * という順で、専門用語を本文で使わない。こちらはタイトルの次がいきなり
 * 見出し数値で、**区画・到達不可・レイヤーがどれも未定義のまま出ていた**。
 *
 * 数値は meta から入れる。ここに直接書くと対象地域を変えたときに古くなる。
 */
export function renderIntro(meta: Meta): void {
  const el = document.getElementById("intro-lead");
  if (!el) return;
  el.innerHTML =
    `東京 ${meta.target_wards.length} 区を 250m 四方の<b>区画</b>` +
    `（${escapeHtml(meta.mesh_label)}）${meta.mesh_count.toLocaleString("ja-JP")} 個に区切り、` +
    "カームダウン・クールダウンスペースを次に検討すべき区画を、" +
    "オープンデータだけから算出した地図です。" +
    "<b>示すのは区画であって、特定の施設ではありません。</b>";
}

/* ------------------------------------------------------------------ スライダー */

export function renderSliders(
  meta: Meta,
  weights: Weights,
  onChange: (key: string, value: number) => void,
): void {
  const host = document.getElementById("sliders")!;
  host.innerHTML = "";

  for (const side of ["demand", "load"] as const) {
    const comps = meta.components.filter((c) => c.side === side);
    const group = document.createElement("div");
    group.className = "slider-group";
    group.innerHTML = `<div class="slider-group-title">
        <span>${side === "demand" ? "需要スコア" : "負荷スコア"}</span>
        <span>${side === "demand" ? "通わざるを得ない量" : "過負荷になり得る量"}</span>
      </div>`;

    for (const c of comps) {
      const wrap = document.createElement("div");
      wrap.className = "slider" + (c.sign < 0 ? " is-negative" : "");
      const id = `w-${c.key}`;
      // 出典と尺度の札は `title` 属性にしか無かった。**タッチ環境では
      // 読めないうえ、8 本のスライダーが「データの層」であることが
      // 画面のどこにも書かれていない**状態になっていた（点の重畳表示の方が
      // レイヤーらしく見える、という取り違えが実際に起きた）。
      const tag = c.absolute
        ? `<span class="scale-tag is-absolute" title="${escapeAttr(c.absolute.basis)}">絶対尺度</span>`
        : '<span class="scale-tag" title="対象地域内での順位。対象地域を変えれば値も変わる。">地域内順位</span>';
      wrap.innerHTML = `
        <div class="slider-head">
          <label for="${id}" title="${escapeAttr(c.rationale)}&#10;&#10;出典: ${escapeAttr(c.source)}">${escapeHtml(c.label)} ${tag}</label>
          <span class="slider-value" id="${id}-val">${fmt(weights[c.key] ?? c.weight, 1)}</span>
        </div>
        <input type="range" id="${id}" min="0" max="2" step="0.1"
               value="${weights[c.key] ?? c.weight}"
               aria-label="${escapeAttr(c.label)} の重み" />
        <div class="factor-source slider-source">出典: ${escapeHtml(c.source)}</div>`;
      group.appendChild(wrap);

      const input = wrap.querySelector<HTMLInputElement>("input")!;
      input.addEventListener("input", () => {
        const v = Number(input.value);
        wrap.querySelector(`#${id}-val`)!.textContent = fmt(v, 1);
        onChange(c.key, v);
      });
    }
    host.appendChild(group);
  }
}

export function syncSliders(weights: Weights, meta: Meta): void {
  for (const c of meta.components) {
    const input = document.getElementById(`w-${c.key}`) as HTMLInputElement | null;
    const val = document.getElementById(`w-${c.key}-val`);
    if (!input) continue;
    const v = weights[c.key] ?? c.weight;
    input.value = String(v);
    if (val) val.textContent = fmt(v, 1);
  }
}

/* ------------------------------------------------------------------ 表示モード */

/**
 * 地図に何を塗るか。
 *
 * 「需要 × 負荷」と主張する以上、掛ける前の 2 つを別々に見せられないと
 * 検証しようがない。需要だけ・負荷だけ・掛け算結果を切り替えて
 * 見比べられることが、このモデルの説明そのものになる。
 */
export const DISPLAY_MODES: {
  id: "priority" | "demand" | "load";
  label: string;
  legend: string;
  /** 凡例の目盛りに書く「この色が何の値か」。 */
  scale: string;
  note: string;
}[] = [
  {
    id: "priority",
    label: "設置優先度",
    legend: "設置優先度（需要 × 負荷）",
    // **「対象地域内での順位」と書いてはいけない。** 2026-08-03 に
    // 優先度から最後のパーセンタイル化を外したので、この色は順位ではなく
    // 掛け算の値そのものになった。需要・負荷の 2 つは順位のままなので、
    // 目盛りの語をモードごとに変える。
    scale: "需要 × 負荷の値",
    note: "需要と負荷の掛け算。両方が揃った場所だけが濃くなる。色は順位ではなく値なので、差が小さい場所は色の差も小さい。",
  },
  {
    id: "demand",
    label: "需要のみ",
    legend: "需要スコア",
    scale: "対象地域内での順位",
    note: "通わざるを得ない人の量だけを見る。住宅地や郊外の通所拠点も濃く出る。",
  },
  {
    id: "load",
    label: "負荷のみ",
    legend: "負荷スコア",
    scale: "対象地域内での順位",
    note: "過負荷になり得る量だけを見る。人のいない工業地帯も濃く出る。",
  },
];

export function renderDisplayModes(
  active: string,
  onPick: (id: "priority" | "demand" | "load") => void,
): void {
  const host = document.getElementById("display-modes")!;
  host.innerHTML = "";
  for (const m of DISPLAY_MODES) {
    const b = document.createElement("button");
    b.className = "preset-btn";
    b.type = "button";
    b.textContent = m.label;
    b.setAttribute("aria-pressed", String(m.id === active));
    b.addEventListener("click", () => onPick(m.id));
    host.appendChild(b);
  }
  const mode = DISPLAY_MODES.find((m) => m.id === active);
  document.getElementById("display-note")!.textContent = mode?.note ?? "";
  const legendTitle = document.querySelector(".legend-title");
  if (legendTitle) legendTitle.textContent = mode?.legend ?? "設置優先度";
  const scaleLabel = document.getElementById("legend-scale-label");
  if (scaleLabel) scaleLabel.textContent = mode?.scale ?? "";
}

/* ------------------------------------------------------------------ プリセット */

/**
 * 順位表に出す件数の切り替え。
 *
 * **恣意性を隠さないために置いている。** 優先度は連続していて、
 * 20 でも 50 でも 100 でも、そこに切れ目があるわけではない。
 * 固定の件数を黙って出すより、動かせるほうが正直である
 *（重みスライダーと同じ考え方）。選択肢は etl/config.py が唯一の出所。
 */
export function renderRankingN(
  meta: Meta,
  active: number,
  onPick: (n: number) => void,
): void {
  const host = document.getElementById("ranking-n");
  if (!host) return;
  host.innerHTML = '<span class="ranking-n-label">表示件数</span>';
  for (const n of meta.ranking_options) {
    const b = document.createElement("button");
    b.className = "preset-btn";
    b.type = "button";
    b.textContent = String(n);
    b.setAttribute("aria-pressed", String(n === active));
    b.addEventListener("click", () => onPick(n));
    host.appendChild(b);
  }
}

export function renderPresets(
  meta: Meta,
  active: string,
  onPick: (id: string) => void,
): void {
  const host = document.getElementById("presets")!;
  host.innerHTML = "";
  for (const p of meta.presets) {
    const b = document.createElement("button");
    b.className = "preset-btn";
    b.type = "button";
    b.textContent = p.label;
    b.setAttribute("aria-pressed", String(p.id === active));
    b.addEventListener("click", () => onPick(p.id));
    host.appendChild(b);
  }
  const note = meta.presets.find((p) => p.id === active)?.note ?? "";
  document.getElementById("preset-note")!.textContent = note;
}

/* ------------------------------------------------------------------ 詳細パネル */

export function renderDetail(
  state: AppState,
  onPick: (meshCode: string, lon?: number, lat?: number) => void,
): void {
  const body = document.getElementById("detail-body")!;
  body.innerHTML = "";

  if (state.tab === "ranking") renderRanking(body, state, onPick);
  else renderSelected(body, state);
}

function renderRanking(
  body: HTMLElement,
  state: AppState,
  onPick: (meshCode: string) => void,
): void {
  const { meta } = state;
  const list = rankingRows(state);

  const intro = document.createElement("p");
  intro.className = "card-narrative";
  intro.style.marginBottom = "10px";
  // **「提言リスト」と「表で見る」を分けていた理由はもう無い。**
  // 提言の単位を区画に戻した時点で、両者は同じ順位表の別表示になった。
  intro.innerHTML =
    `現在の重みでの優先順位です。<b>単位は 250m の区画</b>で、` +
    `全 ${meta.mesh_count.toLocaleString("ja-JP")} 区画から上位 ${list.length} 件を出しています。` +
    "行を選ぶと地図がその区画へ寄ります。根拠文と実数は" +
    "「選択中の区画」タブに出ます。" +
    "<br><br>" +
    "<b>この打ち切りに根拠はありません。</b>優先度は連続していて、" +
    "どこにも切れ目がありません（件数を変えて確かめられます）。" +
    "示すのは区画であって設置先の施設ではありません — この分析は施設の余剰空間も" +
    "運営体制も測っておらず、特定の建物を評価する根拠を持ちません。";
  body.appendChild(intro);

  if (!list.length) {
    body.appendChild(Object.assign(document.createElement("div"), {
      className: "empty",
      textContent: "該当なし",
    }));
    return;
  }

  const table = document.createElement("table");
  table.className = "data-table is-ranking";
  table.innerHTML = `
    <thead>
      <tr>
        <th>順位</th><th>区 / 最寄り駅</th>
        <th class="num">優先度</th><th class="num">需要</th><th class="num">負荷</th>
        <th class="num">公共<br>施設</th><th class="num">接する<br>上位</th>
      </tr>
    </thead>`;

  const tbody = document.createElement("tbody");
  for (const r of list) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    if (state.selected === r.meshCode) tr.style.background = "var(--surface-2)";
    const where = [r.ward, r.station].filter(Boolean).join(" ");
    tr.innerHTML = `
      <td>${r.rank}</td>
      <td>${escapeHtml(where || "—")}
        <span class="factor-source" style="font-family:var(--mono)">${escapeHtml(r.meshCode)}</span></td>
      <td class="num">${fmt(r.priority)}</td>
      <td class="num">${fmt(r.demand)}</td>
      <td class="num">${fmt(r.load)}</td>
      <td class="num">${
        r.unreachable
          ? '<b class="is-unreachable-text">0 件</b>'
          : `${num(r.hostCount)} 件`
      }</td>
      <td class="num">${r.adjacentN}</td>`;
    tr.addEventListener("click", () => onPick(r.meshCode));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);

  const note = document.createElement("p");
  note.className = "card-narrative";
  note.style.marginTop = "10px";
  // **「接する上位区画」の母数を必ず書く。** この数は表示件数で変わる。
  note.innerHTML =
    `「徒歩圏の公共施設」は半径 ${num(meta.host_max_distance_m)}m の件数（<b>0 件 = 到達不可</b>）。` +
    `「接する上位区画」は<b>いま表示している ${list.length} 区画のうち</b>` +
    "この区画に隣り合うものの数です（斜めも隣として数えます）。" +
    "<b>表示件数を変えればこの数も変わります</b> — 場所の性質ではありません。";
  body.appendChild(note);
}

function renderSelected(body: HTMLElement, state: AppState): void {
  const { rows, score, meta, selected, weights } = state;
  if (!selected) {
    body.innerHTML =
      '<div class="empty">地図上の区画、または提言リストの項目を選択してください。</div>';
    return;
  }
  const idx = rows.findIndex((r) => r.c === selected);
  if (idx < 0) {
    body.innerHTML = '<div class="empty">該当する区画が見つかりません。</div>';
    return;
  }

  const row = rows[idx];
  const rank = score.order.indexOf(idx) + 1;

  const head = document.createElement("div");
  head.innerHTML = `
    <h3>区画 <span style="font-family:var(--mono)">${escapeHtml(row.c)}</span></h3>
    <p class="subtitle">メッシュコード（${escapeHtml(meta.mesh_label)}） / 全 ${meta.mesh_count.toLocaleString("ja-JP")} 区画中 第 ${rank} 位</p>
    <div class="card-meta" style="border-top:none;padding-top:0">
      <span>優先度 <b>${fmt(score.priority[idx])}</b></span>
      <span>需要 <b>${fmt(score.demand[idx])}</b></span>
      <span>負荷 <b>${fmt(score.load[idx])}</b></span>
    </div>`;
  body.appendChild(head);

  const narrative = document.createElement("p");
  narrative.className = "card-narrative";
  narrative.style.margin = "10px 0 14px";
  narrative.textContent = narrate(state, idx, rank, weights, meta);
  body.appendChild(narrative);

  // --- 実数（スコアの根拠になる生の数字） ---
  //
  // 節ごとに「どの半径で数えたか」を一度だけ書く。平らな 1 枚の表に戻すと、
  // 半径 800m の件数・最寄り 1 件・区画自身の値・半径 700m の件数が
  // 同じ見た目で並び、読み手が区別できない。
  const sections = factsOf(row, meta.fact_radius_m);
  if (sections.length) {
    const box = document.createElement("div");
    box.innerHTML =
      "<h2>この区画の実数</h2>" +
      sections
        .map(
          (sec) =>
            `<div class="fact-section">
               <div class="fact-section-title">${escapeHtml(sec.title)}</div>
               <div class="factor-source">${escapeHtml(sec.note)}</div>
               <table class="data-table">` +
            sec.items
              .map(
                (f) =>
                  `<tr><th style="text-transform:none;letter-spacing:0">${escapeHtml(f.label)}</th>` +
                  `<td class="num">${escapeHtml(f.value)}</td></tr>`,
              )
              .join("") +
            "</table></div>",
        )
        .join("");
    body.appendChild(box);
  }

  for (const side of ["demand", "load"] as const) {
    const section = document.createElement("div");
    section.className = "factors";
    section.innerHTML = `<h2 style="margin-top:14px">${side === "demand" ? "需要側の内訳" : "負荷側の内訳"}</h2>`;

    const factors = topFactors(row, meta.components, side, weights, 6);
    if (!factors.length) {
      section.innerHTML += '<div class="empty">この側の重みがすべて 0 です。</div>';
      body.appendChild(section);
      continue;
    }

    for (const f of factors) {
      const negative = f.component.sign < 0;
      const el = document.createElement("div");
      el.className = "factor";
      // 数値のすぐ隣で「この 0.89 は何の 0.89 か」を言う。
      // 8 層のうち 2 層（騒音・用途地域）は地域内順位ではない。
      const abs = f.component.absolute;
      const tag = abs
        ? `<span class="scale-tag is-absolute" title="${escapeAttr(abs.label)}&#10;根拠: ${escapeAttr(abs.basis)}">絶対尺度</span>`
        : `<span class="scale-tag" title="${escapeAttr(`${meta.mesh_count.toLocaleString("ja-JP")}区画の中での順位。対象地域を変えれば値も変わる。`)}">地域内順位</span>`;
      el.innerHTML = `
        <div>
          <div class="factor-label">${escapeHtml(f.component.label)} ${tag}</div>
          <div class="factor-bar${negative ? " is-negative" : ""}">
            <i style="width:${Math.round(f.normalized * 100)}%"></i>
          </div>
          <div class="factor-source">${escapeHtml(f.component.source)}</div>
        </div>
        <div class="factor-num">${fmt(f.normalized)}</div>`;
      section.appendChild(el);
    }
    body.appendChild(section);
  }
}

/**
 * 表形式。色に頼らずに順位を読めるようにするためのアクセシビリティ経路であり、
 * 数値をそのまま資料へ転記するための出力でもある。
 */
/* ------------------------------------------------------------------ 補助 */

export function renderBanner(meta: Meta): void {
  const el = document.getElementById("synthetic-banner")!;
  if (!meta.synthetic) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.className = "banner";

  const real = meta.real_layer_count ?? 0;
  const total = meta.layer_total ?? 0;
  const fake = Object.entries(meta.layer_provenance ?? {})
    .filter(([, v]) => v === "synthetic")
    .map(([k]) => k);

  // 全部が模擬か、一部だけ実データが入っているかで文言を変える。
  // 「どこまで本物か」が一目で分かることが、この作品の誠実さの担保になる。
  const heading =
    real > 0
      ? `一部が模擬データ（実データ ${real}/${total} レイヤー）`
      : "模擬データで動作中";

  el.innerHTML = `<span class="banner-icon">⚠</span><span>
      <strong>${heading}</strong>
      ${
        fake.length
          ? `模擬: ${escapeHtml(fake.join("・"))}。これらに由来する数値・施設名は架空で、実際の提言として引用できません。`
          : ""
      }
    </span>`;
}

/**
 * この分析で分からないこと。
 *
 * 普通の作品は「できること」しか書かない。**この作品は「できないこと」を
 * 画面に出す**——文書（docs/issues.md）に全部書いてあるのに画面に無いと、
 * 画面が最も強く主張しているのは網羅性（実データ 10/10）であって
 * 確からしさではない、という状態になる。
 *
 * ここに書くのは「一般論としての限界」ではなく、**この実装で実際に
 * 測っていないもの**に限る。測っていないことの一覧は、
 * 測ったことの一覧と同じだけ具体的でなければ意味がない。
 */
export function renderLimitations(meta: Meta): void {
  const el = document.getElementById("limitations");
  if (!el) return;
  const items: [string, string][] = [
    [
      "施設の適否は測っていない",
      "このツールが述べるのは 250m 区画についてであって、施設についてではない。" +
        "需要も負荷も区画の属性から作っており、余剰空間・運営体制・" +
        "個室の有無といった施設側の条件を測る構成要素は一つも無い。" +
        "供給側から言えるのは「徒歩圏に屋内の公共空間が何件あるか」までである。",
    ],
    [
      "屋内の静けさを測っていない",
      "騒音は幹線道路の道路端 5m で測った屋外の値（要請限度測定）で、" +
        "繁華街の雑踏も、建物の中の音環境も含まない。",
    ],
    [
      "光・匂い・触覚は未評価",
      "感覚過敏の負荷は音だけではないが、面として推定できる" +
        "オープンデータが見つかっていない。用途地域を「法的な騒がしさの上限」" +
        "として代理に使っているのはこの穴を埋めるためで、直接の測定ではない。",
    ],
    [
      "鉄道騒音は入っていない",
      "都の鉄道騒音調査は測定点が疎で、線路からの距離減衰で補完する必要がある。" +
        "補完の妥当性を検証できていないため入れていない。",
    ],
    [
      "混雑は昼間人口ではなく従業者数",
      "昼間人口のメッシュ統計が配信されていないため、経済センサス（2021年）の" +
        "従業者数で代替している。買い物客・通学者・観光客を含まない。",
    ],
    [
      "都・国・民間の施設は供給側に入っていない",
      `徒歩圏${meta.host_max_distance_m}m の判定に使うのは 23 区が公開する` +
        "公共施設一覧のみ。都立施設・駅ナカ・商業施設は数えていないので、" +
        "「到達不可」は過大に出る。北区は区が一覧を公開しておらず特に薄い。",
    ],
    [
      "提言の単位は地区であって地点ではない",
      "上位区画を隣接関係で束ねた「区名＋最寄り駅名」の地区を示す。" +
        "250m 区画の中のどこに置くべきかは、この分析からは言えない。",
    ],
    [
      "順位は特別支援学校というレイヤー 1 枚に強く依存している",
      "23 区に 45 校しかないため、この層は「学校の徒歩圏に入っているか」という" +
        "ゲートとして働く。どの半径で撒くかが「恩恵を受ける区画」を決めてしまい、" +
        "半径 800m ではこの層を外すと上位 10 区画が全部入れ替わる。" +
        "順位そのものを固定の答えとして読まないこと。" +
        "対して「徒歩圏に公共施設が 1 件も無い区画」はこの層と無関係に決まる。",
    ],
    [
      "当事者による検証を経ていない",
      "感覚過敏の当事者に必要な場所を、当事者への聞き取り無しに" +
        "オープンデータだけで推定している。この構造そのものが最大の限界である。",
    ],
  ];

  el.innerHTML =
    '<h2>この分析で分からないこと</h2>' +
    '<ul class="sources">' +
    items
      .map(
        ([title, body]) =>
          `<li><b>${escapeHtml(title)}</b><br>${escapeHtml(body)}</li>`,
      )
      .join("") +
    "</ul>";
}

export function renderLegendNote(meta: Meta): void {
  // 輪郭が 2 種類あることを凡例に書く。書かないと、破線が何を囲んでいるのかが
  // 提言タブの説明文を読んだ人にしか分からない。
  document.getElementById("legend-note")!.innerHTML =
    `${escapeHtml(meta.mesh_label)}・${meta.mesh_count.toLocaleString("ja-JP")}区画。` +
    "<br>実線 = 選択中の区画 / 破線 = その区画が属する地区。";
}

/**
 * 感度分析の結果。「重みは恣意的では?」への定量的な回答。
 *
 * スライダーで確かめられるようにしてあるが、審査員が実際に動かすとは限らない。
 * 動かした結果がどうなるかを、あらかじめ数値で出しておく。
 */
export function renderSensitivity(s: Sensitivity | null, meta?: Meta): void {
  const el = document.getElementById("sensitivity");
  if (!el) return;
  if (!s) {
    el.hidden = true;
    return;
  }

  // sensitivity.json は --sensitivity を付けたビルドでだけ書かれる。
  // つまり現物が mesh.geojson より古いことがあり得るのに、画面はそれを
  // 無言で出していた。**古い感度分析は、無いより悪い。**
  if (meta && s.generated_at && s.generated_at !== meta.generated_at) {
    el.hidden = false;
    el.innerHTML = `
      <p class="card-narrative">
        <b>感度分析は別のビルドの結果です</b>（分析 ${escapeHtml(s.generated_at)} /
        現在のデータ ${escapeHtml(meta.generated_at)}）。
        値が現在の地図と対応しないため表示していません。
        <code>python -m etl.build --live --sensitivity</code> で作り直せます。
      </p>`;
    return;
  }
  el.hidden = false;

  const rp = s.random_perturbation;
  const pa = s.preset_agreement;
  const top10 = rp.overlap_mean["10"] ?? 0;
  // 依存が最も大きい（外すと最も入れ替わる）レイヤー。
  const driver = s.leave_one_out[0];

  el.innerHTML = `
    <div class="stat">
      <div class="stat-value">${Math.round(top10 * 100)}<small>%</small></div>
      <div class="stat-label">
        重みを ±${Math.round(rp.perturbation * 100)}% ランダムに動かしても
        （${rp.trials.toLocaleString("ja-JP")}回試行）上位10区画に残り続けた割合。
        順位の変動は中央値 ${rp.rank_shift_median} 位。
      </div>
    </div>
    <p class="card-narrative" style="margin-top:10px">
      立場の違う ${pa.preset_ids.length} つのプリセット全てで上位${pa.top_k}件に入った区画は
      <b>${pa.common_count}件（${Math.round(pa.common_ratio * 100)}%）</b>。
      ${
        // **0 件のときに「共通して上位に来る場所がある」と書いてはいけない。**
        // 文が数値と逆になる。実際、特別支援学校の帯域を直したらこの値が
        // 3 件 → 0 件になり、画面だけが古い主張を続けた。
        pa.common_count > 0
          ? "重みの選び方に関係なく上位に来る場所がある、ということ。"
          : "<b>重みの選び方に関係なく上位に来る場所は無い</b>——" +
            "どこを優先すべきかは、何を重視するかに依存する。"
      }
    </p>
    <p class="card-narrative">
      最も結果を左右するレイヤーは<b>${escapeHtml(shortLabel({ label: driver.label } as ComponentDef))}</b>で、
      これを外すと上位10件の重なりは ${Math.round(driver.overlap_top10 * 100)}% まで下がる。
      ${
        // **依存が極端なときは、数字を出すだけで終わらせない。**
        // 「40% です」と「全部入れ替わります」は読み手にとって別の話で、
        // 後者なら順位そのものの読み方を変えてもらう必要がある。
        // しきい値ではなく現物の値で分岐させる（層が入れ替わっても効く）。
        driver.overlap_top10 <= 0.3
          ? "<b>つまり、この 1 枚で上位の顔ぶれがほぼ決まっている。</b>" +
            "順位を固定の答えとして読まないこと。" +
            "一方「徒歩圏に公共施設が 1 件も無い区画」は、" +
            "どのレイヤーの重みとも無関係に決まる。"
          : ""
      }
    </p>
    ${renderFixedValues(s)}`;
}

/**
 * 重み以外の固定値を揺さぶった結果。
 *
 * スライダーで動かせるのは 8 レイヤーの重みだけで、**結果を決めている
 * 固定値は他に 76 個ある**（仮定員・種別重み・帯域・用途地域の負荷値・
 * IDW・α/β）。画面が「重みを動かしても変わりません」しか言わないと、
 * 触れない固定値の方が効いていることを隠すことになる。
 */
function renderFixedValues(s: Sensitivity): string {
  const fv = s.fixed_values;
  if (!fv) return "";

  const all = fv.groups.find((g) => g.id === "all");
  // 重い順に並べる。上位10件の重なりが小さいほど、その固定値が効いている。
  const groups = fv.groups
    .filter((g) => g.id !== "all")
    .slice()
    .sort((a, b) => (a.overlap_mean["10"] ?? 1) - (b.overlap_mean["10"] ?? 1));

  const pct = (v: number | undefined) => `${Math.round((v ?? 0) * 100)}%`;

  const rows = groups
    .map(
      (g) =>
        `<tr><th style="text-transform:none;letter-spacing:0">${escapeHtml(g.label)}
           <span class="factor-source">固定値 ${g.constants} 個</span></th>
         <td class="num">${pct(g.overlap_mean["10"])}
           <span class="factor-source">最悪 ${pct(g.overlap_min["10"])}</span></td></tr>`,
    )
    .join("");

  const drop = fv.scenarios.find((x) => x.id === "drop_assumed_capacity");

  return `
    <details style="margin-top:10px">
      <summary>スライダーに出ていない固定値の影響</summary>
      <p class="card-narrative" style="margin-top:8px">
        重みを動かせるのは 8 レイヤーだけですが、結果を決めている固定値は
        他にもあります（徒歩圏の帯域、サービス種別ごとの重み、定員の無い種別に
        当てている仮定員、用途地域から負荷への写像など）。
        これらを ±${Math.round(fv.perturbation * 100)}% 揺さぶったときに
        上位10区画に残り続けた割合です（${fv.trials}回試行・重い順）。
      </p>
      <table class="data-table">${rows}</table>
      ${
        all
          ? `<p class="card-narrative"><b>${all.constants} 個すべてを同時に動かすと
             ${pct(all.overlap_mean["10"])}（最悪 ${pct(all.overlap_min["10"])}）</b>。
             この作品が出せる中で最も不利な数字です。</p>`
          : ""
      }
      ${
        drop
          ? `<p class="card-narrative">定員が公表されない事業所
             ${fv.assumed_capacity_rows.toLocaleString("ja-JP")}件には種別ごとの仮定値を当てています。
             <b>この ${fv.assumed_capacity_rows.toLocaleString("ja-JP")}件を需要から全部落としても、
             上位10区画は ${pct(drop.overlap["10"])} が残ります</b>
             （上位50区画では ${pct(drop.overlap["50"])}）。</p>`
          : ""
      }
    </details>`;
}

/**
 * 各層が「対象地域内の順位」か「外部の基準に固定した絶対尺度」かを示す札。
 *
 * **ここは以前、画面が実装と違うことを断言していた箇所である。**
 * 「各レイヤーは対象地域内のパーセンタイル順位で 0〜1 に正規化しています」と
 * 書いていたが、騒音と用途地域は絶対尺度で、23 区へ広げても値が変わらない。
 * 誤りである以前に、**この 2 層には「順位は相対値」という但し書きが
 * 要らないという有利な事実を、画面が自分で捨てていた。**
 */
function scaleBadge(c: ComponentDef): string {
  if (c.absolute) {
    return (
      `<span class="scale-tag is-absolute" title="${escapeAttr(c.absolute.basis)}">絶対尺度</span>` +
      `<span class="factor-source">${escapeHtml(c.absolute.label)}` +
      `（根拠: ${escapeHtml(c.absolute.basis)}）。対象地域を変えても値が変わりません。</span>`
    );
  }
  return (
    '<span class="scale-tag">地域内順位</span>' +
    '<span class="factor-source">対象地域内での順位を 0〜1 に配分。' +
    "<b>対象地域を変えれば値も変わります。</b></span>"
  );
}

export function renderMethodology(meta: Meta): void {
  const el = document.getElementById("methodology")!;
  const n = meta.mesh_count.toLocaleString("ja-JP");

  const rows = meta.components
    .map(
      (c) =>
        `<li><b>${escapeHtml(c.label)}</b>（${c.side === "demand" ? "需要" : "負荷"}${c.sign < 0 ? "・減点" : ""}）<br>
         ${escapeHtml(c.rationale)}<br>
         ${scaleBadge(c)}<br>
         <span class="factor-source">出典: ${escapeHtml(c.source)}</span></li>`,
    )
    .join("");

  const absolute = meta.components.filter((c) => c.absolute);

  const sources = meta.sources
    .map(
      (s) =>
        `<tr>
           <th style="text-transform:none;letter-spacing:0">
             ${s.url ? `<a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.label)}</a>` : escapeHtml(s.label)}
             <span class="factor-source">${escapeHtml(s.license)}</span>
           </th>
           <td class="num">${escapeHtml(s.vintage)}<br>
             <span class="factor-source">${s.count != null ? `${s.count.toLocaleString("ja-JP")}件` : ""}</span>
           </td>
         </tr>`,
    )
    .join("");

  el.innerHTML = `
    <p class="card-narrative" style="margin-top:8px">
      設置優先度 = 需要スコア × 負荷スコア。足し算ではなく掛け算にすることで、
      「人がいて、かつ過負荷」の両方が揃った場所だけが上位に出ます。
    </p>
    <p class="card-narrative">
      正規化は層によって違います。<b>${meta.components.length - absolute.length} 層</b>は
      対象地域（${n} 区画）内のパーセンタイル順位で 0〜1 に配分するので、
      <b>対象地域を変えれば値も変わります</b>。残る
      <b>${absolute.length} 層</b>（${absolute.map((c) => escapeHtml(c.label.split("（")[0])).join("・")}）は
      法令・告示に 0 と 1 を固定した<b>絶対尺度</b>で、対象地域を変えても値は変わりません。
    </p>
    <ul class="sources">${rows}</ul>

    <h2 style="margin-top:14px">データ出典と年次</h2>
    <p class="card-narrative">
      <b>年次はそろっていません</b>（事業所 2026 年 〜 公園 2011 年）。
      各レイヤーで入手できる最新版を使っています。
    </p>
    <table class="data-table">${sources}</table>
    <p class="card-narrative">
      レジストリには他に ${meta.unused_source_count} 件の出典がありますが、
      <b>検討しただけで使っていない</b>ため、ここには出していません。
    </p>
    <p class="card-narrative">
      データ生成: ${escapeHtml(meta.generated_at)} / モード: ${meta.data_mode}
    </p>`;
}

function escapeHtml(s: string): string {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function escapeAttr(s: string): string {
  return escapeHtml(s).replace(/\n/g, "&#10;");
}

/** 現在の重みでスコアを再計算する。 */
export function recompute(state: AppState): ScoreResult {
  return compose(
    state.rows,
    state.meta.components,
    state.weights,
    state.meta.priority_alpha,
    state.meta.priority_beta,
  );
}
