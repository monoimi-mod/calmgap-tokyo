/**
 * パネル UI の描画。
 *
 * 提言リストはサーバ側の proposals.json をそのまま出すのではなく、
 * 現在のスライダー重みから毎回組み直す。重みを動かしたのに
 * 提言が変わらなければ、このツールは嘘をついていることになる。
 * （proposals.json は既定重みでの静的な書き出しであり、資料添付用に残してある）
 */

import { compose, topFactors, PRESETS, type ScoreResult } from "./score";
import type { ComponentDef, Meta, MeshProps, Weights } from "./types";

export interface AppState {
  meta: Meta;
  rows: MeshProps[];
  weights: Weights;
  score: ScoreResult;
  selected: string | null;
  tab: "proposals" | "selected" | "table";
  activePreset: string;
}

const fmt = (n: number, d = 2) => n.toFixed(d);
const pctRank = (p: number) => Math.max(1, Math.round((1 - p) * 100));

/* ------------------------------------------------------------------ 提言 */

export interface UiProposal {
  hostName: string;
  hostKind: string;
  bestRank: number;
  priority: number;
  meshCodes: string[];
  rowIndex: number;
  narrative: string;
  unreachable: boolean;
}

/**
 * 上位メッシュを設置先の施設単位に束ねる。
 *
 * ひとつの図書館が隣接する複数の高優先度メッシュをまとめて受け持つことは多く、
 * メッシュを羅列すると同じ施設が何度も出てきて提言として読めない。
 * 束ねた件数がそのまま「1 箇所の整備で何メッシュ分に効くか」の説明になる。
 */
export function buildProposals(
  state: AppState,
  topN = 40,
  limit = 12,
): UiProposal[] {
  const { rows, score, meta, weights } = state;
  const byHost = new Map<string, UiProposal>();
  const unreachable: UiProposal[] = [];

  const top = score.order.slice(0, topN);

  top.forEach((idx, i) => {
    const row = rows[idx];
    const rank = i + 1;
    const host = (row.host as string) ?? "";

    if (!host) {
      if (unreachable.length < 3) {
        unreachable.push({
          hostName: "",
          hostKind: "",
          bestRank: rank,
          priority: score.priority[idx],
          meshCodes: [row.c],
          rowIndex: idx,
          narrative: narrate(state, idx, rank, weights, meta),
          unreachable: true,
        });
      }
      return;
    }

    const existing = byHost.get(host);
    if (existing) {
      existing.meshCodes.push(row.c);
    } else {
      byHost.set(host, {
        hostName: host,
        hostKind: (row.host_kind as string) ?? "",
        bestRank: rank,
        priority: score.priority[idx],
        meshCodes: [row.c],
        rowIndex: idx,
        narrative: narrate(state, idx, rank, weights, meta),
        unreachable: false,
      });
    }
  });

  const list = [...byHost.values()].sort((a, b) => a.bestRank - b.bestRank).slice(0, limit);
  return [...list, ...unreachable];
}

/** 予算会議にそのまま出せる日本語の根拠文。etl/hosts.py の _narrative と対応。 */
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

  parts.push(
    `優先度 第${rank}位（対象地域内 上位${pctRank(score.priority[idx])}%）。`,
    `需要スコア ${fmt(score.demand[idx])} / 負荷スコア ${fmt(score.load[idx])}。`,
  );

  const d = topFactors(row, meta.components, "demand", weights, 2);
  const l = topFactors(row, meta.components, "load", weights, 2).filter(
    (f) => f.contribution > 0,
  );

  if (d.length) {
    parts.push(`需要側は${d.map((f) => shortLabel(f.component)).join("・")}が押し上げている。`);
  }
  if (l.length) {
    parts.push(`負荷側は${l.map((f) => shortLabel(f.component)).join("・")}が支配的。`);
  }

  const green = (row.n_green as number) ?? 0;
  if (green < 0.15) {
    parts.push("緑・公園被覆がほぼ無く、屋外に代替の退避先が存在しない。");
  }

  const host = (row.host as string) ?? "";
  if (host) {
    const dist = row.host_d as number | undefined;
    parts.push(
      dist != null
        ? `設置候補: ${host}（メッシュ重心から約${dist}m）。`
        : `設置候補: ${host}。`,
    );
  } else {
    parts.push(
      `半径${meta.host_max_distance_m}m 以内に転用可能な公共施設が無い。` +
        "既存ストックでは到達できず、新規整備または民間施設との連携が要る。",
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
    "転用可能な公共施設が存在しない区画。既存ストックでは到達できず、新規整備が要る。";
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

/* ------------------------------------------------------------------ プリセット */

export function renderPresets(
  active: string,
  onPick: (id: string) => void,
): void {
  const host = document.getElementById("presets")!;
  host.innerHTML = "";
  for (const p of PRESETS) {
    const b = document.createElement("button");
    b.className = "preset-btn";
    b.type = "button";
    b.textContent = p.label;
    b.setAttribute("aria-pressed", String(p.id === active));
    b.addEventListener("click", () => onPick(p.id));
    host.appendChild(b);
  }
  const note = PRESETS.find((p) => p.id === active)?.note ?? "";
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
    "現在の重みでの設置優先順位。1 施設が複数の高優先度メッシュを受け持つ場合はまとめている。";
  body.appendChild(intro);

  if (!proposals.length) {
    body.innerHTML = '<div class="empty">該当なし</div>';
    return;
  }

  proposals.forEach((p) => {
    const el = document.createElement("div");
    el.className = "card";
    if (state.selected && p.meshCodes.includes(state.selected)) el.classList.add("is-active");

    const covered =
      p.meshCodes.length > 1
        ? `<div class="card-meta"><span>この 1 施設で <b>${p.meshCodes.length}</b> メッシュ分をカバー</span></div>`
        : "";

    el.innerHTML = `
      <div class="card-head">
        <span class="rank${p.unreachable ? " is-unreachable" : ""}">${p.bestRank}</span>
        <span class="card-title">${escapeHtml(p.hostName || "候補施設なし（新設が必要）")}</span>
        <span class="card-kind">${escapeHtml(p.hostKind)}</span>
      </div>
      <div class="card-narrative">${escapeHtml(p.narrative)}</div>
      ${covered}`;

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
      el.innerHTML = `
        <div>
          <div class="factor-label">${escapeHtml(f.component.label)}</div>
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
        <th>順位</th><th>メッシュ</th><th>設置候補</th>
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
      <td>${escapeHtml((row.host as string) || "—")}</td>
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
  el.innerHTML = `<span class="banner-icon">⚠</span><span>
      <strong>模擬データで動作中</strong>
      地図上の数値・施設名はすべて架空です。実際の提言として引用できません。
      実データ接続後に <code>python -m etl.build --live</code> で再生成してください。
    </span>`;
}

export function renderLegendNote(meta: Meta): void {
  document.getElementById("legend-note")!.textContent =
    `${meta.mesh_label}・${meta.mesh_count.toLocaleString("ja-JP")}区画。` +
    "色は地域内の相対順位。";
}

export function renderMethodology(meta: Meta): void {
  const el = document.getElementById("methodology")!;
  const rows = meta.components
    .map(
      (c) =>
        `<li><b>${escapeHtml(c.label)}</b>（${c.side === "demand" ? "需要" : "負荷"}${c.sign < 0 ? "・減点" : ""}）<br>
         ${escapeHtml(c.rationale)}<br>
         <span class="factor-source">出典: ${escapeHtml(c.source)}</span></li>`,
    )
    .join("");

  el.innerHTML = `
    <p class="card-narrative" style="margin-top:8px">
      設置優先度 = 需要スコア × 負荷スコア。足し算ではなく掛け算にすることで、
      「人がいて、かつ過負荷」の両方が揃った場所だけが上位に出ます。
      各レイヤーは対象地域内のパーセンタイル順位で 0〜1 に正規化しています。
    </p>
    <ul class="sources">${rows}</ul>
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
