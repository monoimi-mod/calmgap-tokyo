/**
 * スコア合成。etl/score.py の compose() と同一の式でなければならない。
 *
 * 重みをブラウザ側で掛けるのは本作の設計上の中核。
 * 「その重みは恣意的では?」という当然の批判に対して、
 * 審査員自身がスライダーを動かして頑健性を確かめられる状態にしておく。
 * 上位が重みを変えても動かないなら、それはモデルが強いということ。
 *
 * ETL が配信するのは正規化済みの構成要素（n_*）までであり、
 * 最終スコアはここで初めて決まる。
 */

import type { ComponentDef, MeshProps, Weights } from "./types";

/**
 * パーセンタイル順位で 0〜1 に正規化する。
 * 同順位は平均順位を与える（Python の rank(method="average") と同じ）。
 *
 * zeroIsAbsence=true:
 *   0 は「存在しない」。厳密に 0 を返し、正の値だけを (0, 1] に配分する。
 *   事業所が 1 件も無いメッシュを、掛け算モデルで確実に 0 にするため。
 * zeroIsAbsence=false:
 *   全体を [0, 1] に配分する。負値を取り得る連続量（減点レイヤー入りの
 *   負荷の重み付き和）向け。
 *
 * ⚠️ 構成要素そのものの正規化は ETL 側で済んでおり、ここでは行わない。
 *    騒音と用途地域は順位ではなく絶対尺度で正規化されている
 *    （etl/config.py の AbsoluteScale）。n_* をそのまま重み付き加算すること。
 */
export function percentileNormalize(
  values: Float64Array,
  zeroIsAbsence: boolean,
): Float64Array {
  const n = values.length;
  const out = new Float64Array(n);
  if (n === 0) return out;

  // 対象とするインデックス（zeroIsAbsence なら正の値のみ）
  const idx: number[] = [];
  for (let i = 0; i < n; i++) {
    if (!zeroIsAbsence || values[i] > 0) idx.push(i);
  }
  const m = idx.length;
  if (m === 0) return out;
  if (m === 1) {
    out[idx[0]] = 1;
    return out;
  }

  idx.sort((a, b) => values[a] - values[b]);

  // 同値の連なりごとに平均順位（1 始まり）を割り当てる。
  const ranks = new Float64Array(m);
  let i = 0;
  while (i < m) {
    let j = i;
    while (j + 1 < m && values[idx[j + 1]] === values[idx[i]]) j++;
    const avg = (i + 1 + (j + 1)) / 2;
    for (let k = i; k <= j; k++) ranks[k] = avg;
    i = j + 1;
  }

  if (zeroIsAbsence) {
    // (0, 1] へ。最小の正値でも 0（不在）より確実に大きくなる。
    for (let k = 0; k < m; k++) out[idx[k]] = ranks[k] / m;
  } else {
    // 全値が同一なら順位に意味が無い。0 を返す（Python 側と同じ）。
    if (values[idx[0]] === values[idx[m - 1]]) return out;
    for (let k = 0; k < m; k++) out[idx[k]] = (ranks[k] - 1) / (m - 1);
  }
  return out;
}

/** 片側（需要 or 負荷）の重み付き和。減点レイヤーは sign=-1 で効く。 */
function weightedSum(
  rows: MeshProps[],
  components: ComponentDef[],
  weights: Weights,
): { sum: Float64Array; totalAbsWeight: number } {
  const sum = new Float64Array(rows.length);
  let totalAbsWeight = 0;

  for (const c of components) {
    const w = weights[c.key] ?? c.weight;
    if (w === 0) continue;
    totalAbsWeight += Math.abs(w);
    const field = `n_${c.key}`;
    const k = w * c.sign;
    for (let i = 0; i < rows.length; i++) {
      sum[i] += k * ((rows[i][field] as number) ?? 0);
    }
  }
  return { sum, totalAbsWeight };
}

export interface ScoreResult {
  demand: Float64Array;
  load: Float64Array;
  priority: Float64Array;
  /** 表示順を決めるための優先度降順インデックス。 */
  order: number[];
}

/**
 * demand / load / priority を計算する。
 *
 * 手順は etl/score.py compose() と同じ:
 *   1. 需要側・負荷側それぞれ重み付き和
 *   2. 和を再度パーセンタイル正規化して 0〜1 に戻す
 *      （重みの合計が変わっても色のスケールが動かない。
 *        相対比較のツールなので絶対値には意味を持たせない）
 *   3. priority = demand^alpha * load^beta
 *   4. 表示用に priority もパーセンタイル化
 */
export function compose(
  rows: MeshProps[],
  components: ComponentDef[],
  weights: Weights,
  alpha = 1,
  beta = 1,
): ScoreResult {
  const demandComps = components.filter((c) => c.side === "demand");
  const loadComps = components.filter((c) => c.side === "load");

  const d = weightedSum(rows, demandComps, weights);
  const l = weightedSum(rows, loadComps, weights);

  // 片側の重みを全部 0 にした場合、その側は「評価しない」＝中立の 1.0 として
  // 掛け算から外す。0 を返すと全メッシュの優先度が 0 になり地図が消えるため。
  const demand =
    d.totalAbsWeight === 0
      ? new Float64Array(rows.length).fill(1)
      : percentileNormalize(d.sum, true);
  const load =
    l.totalAbsWeight === 0
      ? new Float64Array(rows.length).fill(1)
      : percentileNormalize(l.sum, false);

  // 区単位の補正係数（手帳所持率など）。未設定なら 1.0。
  for (let i = 0; i < rows.length; i++) {
    const coef = rows[i].ward_coefficient;
    if (typeof coef === "number") demand[i] *= coef;
  }

  const priorityRaw = new Float64Array(rows.length);
  for (let i = 0; i < rows.length; i++) {
    priorityRaw[i] = Math.pow(demand[i], alpha) * Math.pow(load[i], beta);
  }
  const priority = percentileNormalize(priorityRaw, true);

  const order = Array.from({ length: rows.length }, (_, i) => i).sort(
    (a, b) => priority[b] - priority[a],
  );

  return { demand, load, priority, order };
}

/** そのメッシュのスコアを押し上げている要因を寄与度順に返す。 */
export function topFactors(
  row: MeshProps,
  components: ComponentDef[],
  side: "demand" | "load",
  weights: Weights,
  k = 3,
) {
  return components
    .filter((c) => c.side === side)
    .map((c) => {
      const w = weights[c.key] ?? c.weight;
      const normalized = (row[`n_${c.key}`] as number) ?? 0;
      return { component: c, normalized, contribution: normalized * w * c.sign };
    })
    .filter((f) => f.contribution !== 0)
    .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
    .slice(0, k);
}
