/**
 * パネル UI の描画。
 *
 * 提言リストはサーバ側の proposals.json をそのまま出すのではなく、
 * 現在のスライダー重みから毎回組み直す。重みを動かしたのに
 * 提言が変わらなければ、このツールは嘘をついていることになる。
 * （proposals.json は既定重みでの静的な書き出しであり、資料添付用に残してある）
 */

import { areaLabel, clusterAdjacent } from "./area";
import { compose, topFactors, type ScoreResult } from "./score";
import type { ComponentDef, Meta, MeshProps, Sensitivity, Weights } from "./types";

export interface AppState {
  meta: Meta;
  rows: MeshProps[];
  weights: Weights;
  score: ScoreResult;
  selected: string | null;
  tab: "proposals" | "selected" | "table";
  activePreset: string;
  /** 地図に塗る値。提言リストの順位は常に優先度で決まる。 */
  displayMode: "priority" | "demand" | "load";
}

const fmt = (n: number, d = 2) => n.toFixed(d);
const pctRank = (p: number) => Math.max(1, Math.round((1 - p) * 100));

/* ------------------------------------------------------------------ 提言 */

export interface UiProposal {
  areaLabel: string;
  wardCounts: Map<string, number>;
  bestRank: number;
  priority: number;
  meshCodes: string[];
  meshCount: number;
  unreachableMeshes: number;
  facilities: { name: string; kind: string; ward: string }[];
  rowIndex: number;
  narrative: string;
  /** 地区の全区画が徒歩圏に公共施設を持たない。 */
  unreachable: boolean;
}

/**
 * 上位メッシュを「隣接する区画のまとまり（地区）」に束ねる。
 *
 * **以前は割当先の施設ごとに束ね、施設名を見出しにしていた。** やめた理由は
 * etl/hosts.py の build_proposals に書いてある（要点: このモデルは施設の
 * 適性を一切測っておらず、見出しの施設はその区画の中に無いことも多い）。
 *
 * 束ねる根拠も「同じ施設が最寄り（最大 700m）」から「格子の上で接している」へ
 * 変えた。後者は整数座標だけで決まり、施設の配置に依存しない。
 */
export function buildProposals(
  state: AppState,
  topN?: number,
  limit?: number,
): UiProposal[] {
  const { rows, score, meta, weights } = state;
  // 母数と上限は etl/config.py が唯一の出所（meta.json 経由）。
  // ここに数値を置くと、資料添付用の proposals.json と画面が別物になる。
  const n = topN ?? meta.proposal_top_n;
  const max = limit ?? meta.proposal_limit;
  const top = score.order.slice(0, n);
  const wardOf = (row: MeshProps) =>
    typeof row.w === "number" ? (meta.target_wards[row.w] ?? "") : "";

  const groups = clusterAdjacent(top.map((idx) => rows[idx].c));

  const list = groups.map((members): UiProposal => {
    const idxs = members.map((m) => top[m]);
    const headIdx = idxs[0];

    const wardCounts = new Map<string, number>();
    for (const i of idxs) {
      const w = wardOf(rows[i]);
      if (w) wardCounts.set(w, (wardCounts.get(w) ?? 0) + 1);
    }

    const { label } = areaLabel(
      idxs.map((i) => wardOf(rows[i])),
      idxs.map((i) => (rows[i].f_station_name as string) ?? ""),
    );

    // 各区画の最寄り施設。設置候補ではなく「徒歩圏に何が在るか」の例示。
    const facilities: UiProposal["facilities"] = [];
    const seen = new Set<string>();
    for (const i of idxs) {
      const name = (rows[i].host as string) ?? "";
      if (name && !seen.has(name)) {
        seen.add(name);
        facilities.push({
          name,
          kind: (rows[i].host_kind as string) ?? "",
          ward: (rows[i].host_ward as string) ?? "",
        });
      }
    }

    const unreachableMeshes = idxs.filter((i) => !rows[i].host).length;

    return {
      areaLabel: label,
      wardCounts,
      bestRank: members[0] + 1,
      priority: score.priority[headIdx],
      meshCodes: idxs.map((i) => rows[i].c),
      meshCount: idxs.length,
      unreachableMeshes,
      facilities,
      rowIndex: headIdx,
      narrative:
        narrate(state, headIdx, members[0] + 1, weights, meta) +
        clusterNarrative(idxs.length, wardCounts, unreachableMeshes, facilities),
      unreachable: unreachableMeshes === idxs.length,
    };
  });

  return list.sort((a, b) => a.bestRank - b.bestRank).slice(0, max);
}

/**
 * 地区としてまとまって初めて言えることだけを足す。
 * 1 区画ずつ眺めても出てこない情報 — 何区画続いているか、区をまたぐか、
 * そのうち何区画が徒歩圏に施設を持たないか。etl/hosts.py の
 * _cluster_narrative と対応。
 */
function clusterNarrative(
  n: number,
  wardCounts: Map<string, number>,
  unreachableMeshes: number,
  facilities: UiProposal["facilities"],
): string {
  const parts: string[] = [];

  if (n > 1) parts.push(`隣接する${n}区画がまとまって上位に入っている。`);

  if (wardCounts.size > 1) {
    const breakdown = [...wardCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([w, c]) => `${w}${c}区画`)
      .join("・");
    parts.push(`この地区は${breakdown}にまたがり、提言先の自治体が分かれる。`);
  }

  if (unreachableMeshes === n && n > 1) {
    parts.push("全区画が徒歩圏に区の公共施設を持たない。");
  } else if (unreachableMeshes) {
    parts.push(`うち${unreachableMeshes}区画は徒歩圏に区の公共施設が無い。`);
  }

  // 施設名は「1 区画だけの地区」なら区画側の文が既に挙げているので繰り返さない。
  if (facilities.length > 1) {
    const names = facilities
      .slice(0, 3)
      .map((f) => `${f.name}（${f.kind}）`)
      .join("、");
    const more = facilities.length > 3 ? " ほか" : "";
    parts.push(
      `各区画から最も近い施設は重複を除いて${facilities.length}件（${names}${more}）。`,
    );
  }
  if (facilities.length) {
    const other = [
      ...new Set(facilities.map((f) => f.ward).filter((w) => w && !wardCounts.has(w))),
    ].sort();
    if (other.length) {
      parts.push(`うち${other.join("・")}の施設が含まれ、区境をまたぐ連携が前提になる。`);
    }
  }

  return parts.join("");
}

const num = (n: number) => n.toLocaleString("ja-JP");

/**
 * そのメッシュの「実数」。ETL が f_* として配信している表示専用の値。
 *
 * スコアは順位に正規化された相対値なので、それ単体では提言文にならない。
 * 「需要 0.94」ではなく「徒歩圏に事業所 4 件・定員 77 人」と書けて初めて
 * 予算会議の資料になる。
 */
export function factsOf(row: MeshProps): { label: string; value: string }[] {
  const f: { label: string; value: string }[] = [];
  const g = (k: string) => row[k] as number | undefined;
  const s = (k: string) => row[k] as string | undefined;

  if (g("f_welfare_n")) {
    const cap = g("f_welfare_cap");
    f.push({
      label: "徒歩圏の障害福祉サービス事業所",
      value: `${num(g("f_welfare_n")!)}件${cap ? `（定員 ${num(cap)}人）` : ""}`,
    });
  }
  if (g("f_school_n")) {
    f.push({ label: "特別支援学校", value: `${num(g("f_school_n")!)}校` });
  }
  if (g("f_clinic_n")) {
    f.push({ label: "精神科・心療内科", value: `${num(g("f_clinic_n")!)}件` });
  }
  if (s("f_station_name")) {
    const r = g("f_station_riders");
    const d = g("f_station_dist");
    f.push({
      label: "最寄り駅",
      value:
        `${s("f_station_name")}` +
        (r ? `（乗降 ${num(r)}人/日）` : "") +
        (d != null ? ` ${num(d)}m` : ""),
    });
  }
  if (s("f_zoning_name")) {
    f.push({ label: "用途地域", value: s("f_zoning_name")! });
  }
  if (g("f_noise_db")) {
    f.push({ label: "推定騒音", value: `${g("f_noise_db")} dB (LAeq)` });
  }
  f.push({
    label: "緑・公園被覆",
    value: g("f_green_pct") ? `${g("f_green_pct")}%` : "0%（屋外に退避先なし）",
  });
  // 供給側について言える唯一の実数。数えているのは施設一覧の行数であって
  // 建物の数ではない（同じ建物の別種別が別行で載っている。docs/issues.md）。
  f.push({
    label: "徒歩圏の公共施設",
    value: g("f_host_n") ? `${num(g("f_host_n")!)}件（一覧の行数）` : "0件",
  });
  return f;
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
      `（上位${pctRank(score.priority[idx])}%）。`,
    `需要 ${fmt(score.demand[idx])} × 負荷 ${fmt(score.load[idx])}` +
      "（いずれも対象地域内での相対値で、絶対的な水準ではない）。",
  );

  // --- 需要側を実数で述べる ---
  const demandBits: string[] = [];
  const wn = row.f_welfare_n as number | undefined;
  const wc = row.f_welfare_cap as number | undefined;
  if (wn) {
    demandBits.push(
      `徒歩圏に障害福祉サービス事業所${num(wn)}件` + (wc ? `（定員計${num(wc)}人）` : ""),
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
 * 「殺し文句」となる 1 数値。
 * 上位メッシュのうち、既存の公共施設では届かないものの割合。
 * これはヒートマップを眺めても出てこない、集計して初めて言える事実。
 */
export function renderStat(state: AppState): void {
  const { rows, score, meta } = state;
  const N = Math.min(50, rows.length);
  const top = score.order.slice(0, N);
  const uncovered = top.filter((i) => !rows[i].host).length;

  const valueEl = document.getElementById("stat-value")!;
  const labelEl = document.getElementById("stat-label")!;

  valueEl.innerHTML = `${uncovered}<small> / ${N} メッシュ</small>`;
  labelEl.textContent =
    `優先度上位${N}メッシュのうち、半径${meta.host_max_distance_m}m 以内に` +
    "区の公共施設が 1 件も無い区画。既存ストックの徒歩圏から外れており、" +
    "新規整備か民間施設との連携が要る。";
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
      wrap.innerHTML = `
        <div class="slider-head">
          <label for="${id}" title="${escapeAttr(c.rationale)}&#10;&#10;出典: ${escapeAttr(c.source)}">${c.label}</label>
          <span class="slider-value" id="${id}-val">${fmt(weights[c.key] ?? c.weight, 1)}</span>
        </div>
        <input type="range" id="${id}" min="0" max="2" step="0.1"
               value="${weights[c.key] ?? c.weight}"
               aria-label="${escapeAttr(c.label)} の重み" />`;
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
  note: string;
}[] = [
  {
    id: "priority",
    label: "設置優先度",
    legend: "設置優先度（需要 × 負荷）",
    note: "需要と負荷の掛け算。両方が揃った場所だけが濃くなる。",
  },
  {
    id: "demand",
    label: "需要のみ",
    legend: "需要スコア",
    note: "通わざるを得ない人の量だけを見る。住宅地や郊外の通所拠点も濃く出る。",
  },
  {
    id: "load",
    label: "負荷のみ",
    legend: "負荷スコア",
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
  document.getElementById("display-note")!.textContent =
    DISPLAY_MODES.find((m) => m.id === active)?.note ?? "";
  const legendTitle = document.querySelector(".legend-title");
  if (legendTitle) {
    legendTitle.textContent =
      DISPLAY_MODES.find((m) => m.id === active)?.legend ?? "設置優先度";
  }
}

/* ------------------------------------------------------------------ プリセット */

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

  if (state.tab === "proposals") renderProposals(body, state, onPick);
  else if (state.tab === "selected") renderSelected(body, state);
  else renderTable(body, state, onPick);
}

function renderProposals(
  body: HTMLElement,
  state: AppState,
  onPick: (meshCode: string) => void,
): void {
  const proposals = buildProposals(state);
  const intro = document.createElement("p");
  intro.className = "card-narrative";
  intro.style.marginBottom = "12px";
  intro.textContent =
    "現在の重みでの優先順位を、隣接する区画のまとまり（地区）ごとに示す。" +
    "示すのは区画であって設置先の施設ではない — この分析は施設の余剰空間も" +
    "運営体制も測っておらず、特定の建物を評価する根拠を持たない。" +
    "挙げている施設名は「徒歩圏に屋内の公共空間が在るか」の例示。";
  body.appendChild(intro);

  if (!proposals.length) {
    body.innerHTML = '<div class="empty">該当なし</div>';
    return;
  }

  proposals.forEach((p) => {
    const el = document.createElement("div");
    el.className = "card";
    if (state.selected && p.meshCodes.includes(state.selected)) el.classList.add("is-active");

    const meta: string[] = [];
    if (p.meshCount > 1) meta.push(`隣接 <b>${p.meshCount}</b> 区画`);
    if (p.unreachableMeshes) {
      meta.push(`徒歩圏に公共施設が無い区画 <b>${p.unreachableMeshes}</b>`);
    }
    const metaRow = meta.length
      ? `<div class="card-meta">${meta.map((m) => `<span>${m}</span>`).join("")}</div>`
      : "";

    // 見出しの右肩は施設の種別ではなく、またがる区の数。提言先が幾つに
    // 分かれるかが、地区単位で見たときに最初に効いてくる情報。
    const wards =
      p.wardCounts.size > 1 ? `${p.wardCounts.size} 区にまたがる` : "";

    el.innerHTML = `
      <div class="card-head">
        <span class="rank${p.unreachable ? " is-unreachable" : ""}">${p.bestRank}</span>
        <span class="card-title">${escapeHtml(p.areaLabel || "地区名なし")}</span>
        <span class="card-kind">${escapeHtml(wards)}</span>
      </div>
      <div class="card-narrative">${escapeHtml(p.narrative)}</div>
      ${metaRow}`;

    el.addEventListener("click", () => onPick(p.meshCodes[0]));
    body.appendChild(el);
  });
}

function renderSelected(body: HTMLElement, state: AppState): void {
  const { rows, score, meta, selected, weights } = state;
  if (!selected) {
    body.innerHTML =
      '<div class="empty">地図上のメッシュ、または提言リストの項目を選択してください。</div>';
    return;
  }
  const idx = rows.findIndex((r) => r.c === selected);
  if (idx < 0) {
    body.innerHTML = '<div class="empty">該当メッシュが見つかりません。</div>';
    return;
  }

  const row = rows[idx];
  const rank = score.order.indexOf(idx) + 1;

  const head = document.createElement("div");
  head.innerHTML = `
    <h3>メッシュ ${escapeHtml(row.c)}</h3>
    <p class="subtitle">${escapeHtml(meta.mesh_label)} / 全 ${meta.mesh_count.toLocaleString("ja-JP")} 区画中 第 ${rank} 位</p>
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
  const facts = factsOf(row);
  if (facts.length) {
    const box = document.createElement("div");
    box.innerHTML =
      '<h2>このメッシュの実数</h2>' +
      '<table class="data-table">' +
      facts
        .map(
          (f) =>
            `<tr><th style="text-transform:none;letter-spacing:0">${escapeHtml(f.label)}</th>` +
            `<td class="num">${escapeHtml(f.value)}</td></tr>`,
        )
        .join("") +
      "</table>";
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
function renderTable(
  body: HTMLElement,
  state: AppState,
  onPick: (meshCode: string) => void,
): void {
  const { rows, score, meta } = state;
  const top = score.order.slice(0, 60);

  const table = document.createElement("table");
  table.className = "data-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>順位</th><th>メッシュ</th><th>区</th><th>徒歩圏の公共施設</th>
        <th class="num">優先度</th><th class="num">需要</th><th class="num">負荷</th>
      </tr>
    </thead>`;

  const tbody = document.createElement("tbody");
  top.forEach((idx, i) => {
    const row = rows[idx];
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    if (state.selected === row.c) tr.style.background = "var(--surface-2)";
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td style="font-family:var(--mono)">${escapeHtml(row.c)}</td>
      <td>${escapeHtml(typeof row.w === "number" ? (meta.target_wards[row.w] ?? "—") : "—")}</td>
      <td class="num">${row.host ? `${num((row.f_host_n as number) ?? 0)} 件` : "0 件"}</td>
      <td class="num">${fmt(score.priority[idx])}</td>
      <td class="num">${fmt(score.demand[idx])}</td>
      <td class="num">${fmt(score.load[idx])}</td>`;
    tr.addEventListener("click", () => onPick(row.c));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  const note = document.createElement("p");
  note.className = "card-narrative";
  note.style.marginBottom = "10px";
  note.textContent = `現在の重みでの上位 ${top.length} メッシュ。${meta.mesh_label}。`;
  body.appendChild(note);
  body.appendChild(table);
}

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
  document.getElementById("legend-note")!.textContent =
    `${meta.mesh_label}・${meta.mesh_count.toLocaleString("ja-JP")}区画。` +
    "色は地域内の相対順位。";
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
