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
  /**
   * 地図に光らせている「徒歩圏に在るもの」の種別。null なら消灯。
   * **数えたものと光らせるものが同じであることが前提**
   *（tools/facility_parity.mjs が全 9,507 区画で検査している）。
   */
  highlight: HighlightKind | null;
  /**
   * いま光らせている点そのもの。一覧をここから作る。
   *
   * **main.ts の highlightFor() が唯一の出所で、地図に渡すのと同じ配列を
   * そのまま受け取る。** ここで数え直したり絞り直したりしてはいけない——
   * そうした瞬間に「表の件数・地図の点・一覧の行数」が 3 者ばらばらに
   * ずれ得るものになる。同じ配列を配れば、ずれる余地が構造的に無い
   *（`tools/facility_parity.mjs` が表と地図の一致を検査しており、
   * 一覧はその地図側と同一物である）。
   */
  highlightPoints: GeoJSON.FeatureCollection | null;
}

/**
 * ハイライトできる実数の種別。etl の f_* と 1 対 1。
 *
 * **`station` と `station_nearest` は別物。** 前者は帯域 600m 以内の全駅
 *（＝スコアが足しているもの）、後者は 1,500m 以内の最寄り 1 駅
 *（＝区画の呼び名）。かつては後者しか無く、`station` という名前で
 * 1 駅だけを指していた。
 */
export type HighlightKind =
  | "welfare"
  | "school"
  | "clinic"
  | "host"
  | "station"
  | "station_nearest";

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

export interface FactItem {
  label: string;
  value: string;
  /** 地図に光らせられる行。数えた半径も一緒に持つ（円を描くため）。 */
  hl?: { kind: HighlightKind; radiusM: number };
}

export interface FactSection {
  title: string;
  /** その節の値が何を数えたものかを一度だけ言う。行ごとに繰り返さない。 */
  note: string;
  items: FactItem[];
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
  const walk: (FactItem & { r: number })[] = [];
  if (g("f_welfare_n")) {
    const cap = g("f_welfare_cap");
    // 徒歩圏は区界で切っていない（エッジ効果を避けるため入力を区界の外側
    // 2km まで拾う設計）。**その事実をカードに出す**——外周のメッシュは
    // 「隣の市の事業所」で需要が決まっていることがある（docs/issues.md B1）。
    const outside = g("f_welfare_outside_n") ?? 0;
    walk.push({
      label: "障害福祉サービス事業所",
      r: radius.welfare,
      hl: { kind: "welfare", radiusM: radius.welfare },
      value:
        `${num(g("f_welfare_n")!)}件${cap ? `（定員 ${num(cap)}人）` : ""}` +
        (outside ? ` ※うち対象23区の外 ${num(outside)}件` : ""),
    });
  }
  if (g("f_school_n")) {
    walk.push({
      label: "特別支援学校",
      r: radius.school,
      hl: { kind: "school", radiusM: radius.school },
      value: `${num(g("f_school_n")!)}校`,
    });
  }
  if (g("f_clinic_n")) {
    walk.push({
      label: "精神科・心療内科",
      r: radius.clinic,
      hl: { kind: "clinic", radiusM: radius.clinic },
      value: `${num(g("f_clinic_n")!)}件`,
    });
  }
  // **駅も件数で出す。** かつては「1,500m 以内の最寄り 1 駅」しか無く、
  // 事業所 60 件・学校 1 校…と件数が並ぶ中で駅だけ 1 件に見えていた。
  // スコア（`station_flow`）は最寄り 1 駅など見ておらず、帯域 600m の
  // カーネルで**周囲の全駅を足している**。半径はその帯域に揃える
  // （他の 3 層が 800m を出しているのと同じ規則。`meta.fact_radius_m`）。
  if (g("f_station_n")) {
    const sum = g("f_station_sum");
    walk.push({
      label: "駅",
      r: radius.station,
      hl: { kind: "station", radiusM: radius.station },
      value:
        `${num(g("f_station_n")!)}駅` +
        (sum ? `（乗降計 ${num(sum)}人/日）` : ""),
    });
  }

  const nearest: FactItem[] = [];
  if (s("f_station_name")) {
    const r = g("f_station_riders");
    const d = g("f_station_dist");
    nearest.push({
      label: s("f_station_name")!,
      // 最寄り 1 駅なので円は描かない（半径 0 = 円なし）。
      hl: { kind: "station_nearest", radiusM: 0 },
      value:
        (r ? `乗降 ${num(r)}人/日` : "乗降規模不明") +
        (d != null ? ` / ${num(d)}m` : ""),
    });
  }

  const own: FactItem[] = [];
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
  const host: FactItem[] = [
    {
      label: "区の公共施設",
      value: g("f_host_n") ? `${num(g("f_host_n")!)}件（一覧の行数）` : "0件",
      ...(g("f_host_n") ? { hl: { kind: "host" as const, radiusM: radius.host } } : {}),
    },
  ];

  const walkSections: FactSection[] = [...new Set(walk.map((w) => w.r))]
    .sort((a, b) => a - b)
    .map((r) => ({
      title: `徒歩圏（半径 ${num(r)}m）にあるもの`,
      // **半径が層で違う理由を、その半径のすぐ隣で言う。**
      // 「なぜ半径がバラバラなのか」は当然の疑問で、答えは
      // 「各層がスコア計算で使っている帯域をそのまま出しているから」。
      // 表示のために別の半径を選ぶと、画面の件数とスコアの根拠が
      // 別の範囲を指すことになる（駅が実際そうなっていた）。
      note:
        `この区画の中心から ${num(r)}m 以内にある件数です。` +
        "区界では切っていないため、隣の自治体の施設も含みます。" +
        `この ${num(r)}m は、スコアがこの層に使っている帯域と同じ値です` +
        "（層ごとに違うのはそのためで、表示のために選んだ数字ではありません）。",
      items: walk.filter((w) => w.r === r).map(({ label, value, hl }) => ({ label, value, hl })),
    }));

  return [
    ...walkSections,
    {
      title: "この区画の呼び名",
      // **役割が違うことを書く。** ここだけ半径が 1,500m なのは、
      // これが需要の実数ではなく**区画に付ける名前**だから
      //（順位表の見出し「豊島区 大塚」の「大塚」がこれ）。
      // 帯域の 600m に揃えると 9,507 区画のうち 4,197 件（44.1%）が
      // 名前を失うので、ここは別の半径のままにしてある。
      note:
        `順位表の見出しに使う最寄り駅です（${num(radius.station_max)}m 以内で` +
        "最も近い 1 駅）。これは区画に付ける名前で、需要の実数ではありません" +
        `——需要に効いている駅は上の「徒歩圏（半径 ${num(radius.station)}m）」の方です。`,
      items: nearest,
    },
    {
      title: "この区画そのものの値",
      note: "周囲を数えたものではなく、区画自身に与えられた値です。",
      items: own,
    },
    {
      title: `既存の公共施設（半径 ${num(radius.host)}m）`,
      // **ここだけ「帯域」ではない。** 上の徒歩圏は各層がスコアに使っている
      // 帯域だが、この 700m はスコアに一切入らない——「徒歩圏に 1 件も無い＝
      // 到達不可」という**主張の定義そのもの**である。種類の違う数字なので、
      // 揃えずに、揃っていない理由を書く。
      note:
        `この ${num(radius.host)}m だけは帯域ではなく、「徒歩圏に 1 件も無い＝到達不可」` +
        "という判定の境目です（スコアには一切入りません）。" +
        "上の徒歩圏とは種類の違う距離なので揃えていません。" +
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
  // **根拠文でも「最寄り 1 駅」ではなく件数で述べる。**
  // ここが「最寄りの大塚は乗降108,702人/日」だったが、スコアが見ているのは
  // 帯域 600m 内の全駅の合計であって最寄り 1 駅ではない。
  // 1 駅だけ書くと、駅が 4 つある区画の需要を 1 駅で説明したことになる。
  const stN = row.f_station_n as number | undefined;
  const stSum = row.f_station_sum as number | undefined;
  const stName = row.f_station_name as string | undefined;
  if (stN) {
    demandBits.push(
      `駅${num(stN)}駅` +
        (stSum ? `（乗降計${num(stSum)}人/日` : "") +
        (stSum && stName ? `・最寄りは${stName}` : "") +
        (stSum ? "）" : ""),
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

  // **見出しに置く数字を 1,058 から 151 へ入れ替えた**（2026-08-04）。
  //
  // 1,058 区画（11.1%）は画面でいちばん大きく出ていたが、
  // **その中身は優先度の下位に固まっている**——順位の中央値は
  // 9,507 件中 8,052 位で、上位 100 区画には 1 件も入らない。
  // 皇居・羽田空港・埋立地・河川敷が主成分だからで、これは
  // 既知の性質として issues.md に書いてあったのに、
  // **画面での扱いだけが「いちばん大きい発見」のままだった。**
  //
  // 代わりに置くのは「区内で優先度が中位以上の到達不可区画」。
  // 非市街地の面積比で決まってしまう部分を落としてあり、
  // **区をまたいで並べてよいのはこちらだけ**である（issues.md A9）。
  //
  // **格下げであって撤去ではない。** この指標は重みもスコアも帯域も
  // 通っていない——この作品でいちばん頑健な出力で、順位（特別支援学校
  // 1 層で全部入れ替わる）とはそこが違う。1,058 は補足として残す。
  kickerEl.textContent =
    "区内で優先度が中位以上で、かつ" +
    `徒歩圏（半径 ${num(meta.host_max_distance_m)}m）に区の公共施設が 1 件も無い区画`;

  valueEl.innerHTML =
    `${u.mid_or_above.toLocaleString("ja-JP")}<small> 区画</small>`;

  labelEl.innerHTML =
    "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要る区画です。" +
    "<b>この数字は重みにもスコアにも依存しません</b>" +
    "（スライダーを動かしても変わりません）。" +
    "しきい値は区ごとの優先度の中央値で、母数は区内の全区画です。" +
    `<br><br>徒歩圏に公共施設が無い区画は、全体では ` +
    `<b>${u.count.toLocaleString("ja-JP")} / ${meta.mesh_count.toLocaleString("ja-JP")} 区画` +
    `（${Math.round(u.ratio * 1000) / 10}%）</b>。` +
    "ただし<b>その大半は優先度の下位</b>で、皇居・羽田空港・埋立地・河川敷など" +
    "人のいない場所が主成分です。" +
    "<b>区ごとにこの割合を出して並べてはいけません</b>——" +
    "非市街地の面積比でほぼ決まってしまいます。" +
    `<br><br>現在の重みでの上位 ${N} 区画のうち、公共施設が徒歩圏に無いものは ${uncoveredTop} 件。` +
    '<br><span class="stat-hint">地図の「重ねる」で該当区画を表示できます。</span>';
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
      const tag = scaleTag(c);
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
  const preset = meta.presets.find((p) => p.id === active);
  const noteEl = document.getElementById("preset-note")!;
  if (!preset) {
    noteEl.textContent = "";
    return;
  }
  // note には **強調** を書ける（etl/config.py 側で書きやすいため）。
  // エスケープしてから太字だけ戻す。
  const note = escapeHtml(preset.note).replace(
    /\*\*(.+?)\*\*/g,
    "<b>$1</b>",
  );

  // **既定から何がどれだけ動くかを見せる。** かつてはプリセットを押すと
  // 8 本のスライダーが黙って入れ替わるだけで、「立場が変われば重みも変わる」
  // という主張のうち **変わったこと自体**が画面に出ていなかった。
  const diff = meta.components
    .map((c) => ({
      label: c.label.split("（")[0],
      from: c.weight,
      to: preset.weights[c.key] ?? c.weight,
    }))
    .filter((d) => Math.abs(d.to - d.from) > 1e-9)
    .sort((a, b) => Math.abs(b.to - b.from) - Math.abs(a.to - a.from));

  const diffHtml = diff.length
    ? '<div class="preset-diff">既定からの変更:' +
      diff
        .map(
          (d) =>
            `<span><i class="${d.to > d.from ? "up" : "down"}">${
              d.to > d.from ? "↑" : "↓"
            }</i>${escapeHtml(d.label)} ${fmt(d.from, 1)}→${fmt(d.to, 1)}</span>`,
        )
        .join("") +
      "</div>"
    : "";

  noteEl.innerHTML = note + diffHtml;
}

/**
 * 「立場を変えると上位が入れ替わる」ことを、プリセットのすぐ隣で数値にする。
 *
 * 感度分析の中に埋もれていると、プリセットを押した人はそこまで辿り着かない。
 * **値は sensitivity.json から取る**（TypeScript に書くと古くなる）。
 */
export function renderPresetAgreement(s: Sensitivity | null, meta: Meta): void {
  const el = document.getElementById("preset-agreement");
  if (!el) return;
  if (!s || (s.generated_at && s.generated_at !== meta.generated_at)) {
    el.hidden = true;
    return;
  }
  const pa = s.preset_agreement;
  el.hidden = false;
  el.innerHTML =
    `この ${pa.preset_ids.length} つ全てで上位${pa.top_k}件に入った区画は ` +
    `<b>${pa.common_count} 件</b>。` +
    (pa.common_count > 0
      ? "重みの選び方に関係なく上位に来る場所がある、ということです。"
      : "<b>重みの選び方に関係なく上位に来る場所はありません</b>——" +
        "どこを優先すべきかは、何を重視するかに依存します。");
}

/* ------------------------------------------------------------------ 詳細パネル */

export function renderDetail(
  state: AppState,
  onPick: (meshCode: string, lon?: number, lat?: number) => void,
  onHighlight: (kind: HighlightKind | null) => void,
  onFocusPoint: (lon: number, lat: number) => void = () => {},
): void {
  const body = document.getElementById("detail-body")!;
  body.innerHTML = "";

  if (state.tab === "ranking") renderRanking(body, state, onPick);
  else renderSelected(body, state, onHighlight, onFocusPoint);
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

/**
 * 規模の欄の書き分け。map.ts の CAPACITY_LABEL と対になっている。
 *
 * **同じ `capacity` という列に、在籍者数（人）・定員（人）・
 * 乗降客数（人/日）が入っている。** 単位を落とすと「規模 131」になり、
 * 何の 131 なのかが消える。クリニックは全件 1（存在フラグ）なので出さない。
 */
const LIST_CAPACITY: Record<
  string,
  { label: string; unit: string; assumed?: string } | null
> = {
  school: { label: "在籍者数", unit: "人", assumed: "在籍者数が未公表・既定値" },
  welfare: { label: "定員", unit: "人", assumed: "届出に定員の記載なし・種別ごとの既定値" },
  station: { label: "乗降客数", unit: "人/日" },
  clinic: null,
};

/**
 * 開いている行の直下に差し込む施設一覧（`<tr>` 1 行に押し込む）。
 *
 * **並べるのは `state.highlightPoints` そのもので、ここで数え直さない。**
 * 地図へ渡すのと同一の配列なので、一覧の行数・地図の点の数・表の件数が
 * 構造的にずれ得ない（`tools/facility_parity.mjs` が後ろ 2 つの一致を
 * 全 9,507 区画で検査しており、一覧はその地図側と同じもの）。
 *
 * 並び順は区画の中心からの距離の昇順。**距離は Python が数えるのと同じ
 * 平面直角座標（x/y・mx/my）で出す**——緯度経度から測ると 800m の境界付近が
 * ずれる（CLAUDE.md「数えたものと光らせるもの」）。
 */
function facilityListRow(state: AppState, row: MeshProps): string {
  const fc = state.highlightPoints;
  if (!fc || !fc.features.length) return "";

  const mx = row.mx as number | undefined;
  const my = row.my as number | undefined;

  const items = fc.features
    .map((f) => {
      const p = (f.properties ?? {}) as Record<string, unknown>;
      const x = p.x as number | undefined;
      const y = p.y as number | undefined;
      const d =
        typeof mx === "number" && typeof my === "number" &&
        typeof x === "number" && typeof y === "number"
          ? Math.hypot(x - mx, y - my)
          : null;
      const g = f.geometry;
      const c = g && g.type === "Point" ? (g.coordinates as number[]) : null;
      return { p, d, lon: c?.[0] ?? null, lat: c?.[1] ?? null };
    })
    .sort((a, b) => (a.d ?? Infinity) - (b.d ?? Infinity));

  const rows = items
    .map(({ p, d, lon, lat }) => {
      const spec = LIST_CAPACITY[String(p.layer ?? "")];
      let size = "";
      if (spec && p.capacity != null) {
        const n = Number(p.capacity).toLocaleString("ja-JP");
        // 仮の値であることを、値と同じ行に書く。
        // 一覧で落とすと、60 行のうちどれが実測か分からなくなる。
        const mark =
          p.assumed && spec.assumed
            ? `<span class="fl-assumed">仮（${escapeHtml(spec.assumed)}）</span>`
            : "";
        size = `<span class="fl-size">${escapeHtml(spec.label)} ${n}${escapeHtml(spec.unit)}</span>${mark}`;
      }
      const focus =
        lon != null && lat != null ? ` data-lon="${lon}" data-lat="${lat}"` : "";
      return `<li class="fl-item"${focus} role="button" tabindex="0">
          <div class="fl-name">${escapeHtml(String(p.name ?? ""))}</div>
          <div class="fl-meta">${escapeHtml(String(p.host_kind ?? p.kind ?? ""))}${
            d != null ? ` ・ ${Math.round(d).toLocaleString("ja-JP")}m` : ""
          }</div>
          ${size ? `<div class="fl-size-row">${size}</div>` : ""}
        </li>`;
    })
    .join("");

  // 出典は一覧の末尾に一度だけ。行ごとに繰り返すと 60 回同じ文字列が並ぶ。
  const sources = [
    ...new Set(fc.features.map((f) => String((f.properties as { source?: string })?.source ?? ""))),
  ].filter(Boolean);

  return `<tr class="fact-list"><td colspan="2">
      <div class="fl-head">${fc.features.length.toLocaleString("ja-JP")} 件（地図に出ている点と同じ）・近い順</div>
      <ul class="fl-list">${rows}</ul>
      ${sources.length ? `<div class="fl-source">出典: ${escapeHtml(sources.join(" / "))}</div>` : ""}
    </td></tr>`;
}

function renderSelected(
  body: HTMLElement,
  state: AppState,
  onHighlight: (kind: HighlightKind | null) => void,
  onFocusPoint: (lon: number, lat: number) => void,
): void {
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
    </div>
    <!--
      **合成後の 2 値にも尺度の札を出す。** 各レイヤーには「地域内順位／
      絶対尺度」の札を出していたのに、ここだけ無かった（docs/issues.md B5）。
      需要と負荷は重み付き和をもう一度パーセンタイル順位に変換した値で、
      **量ではなく順位**である。優先度だけがその掛け算の生値。
    -->
    <p class="scale-note">
      <span class="scale-tag" title="重み付き和を 9,507 区画の中での順位に変換した値。対象地域を変えれば値も変わる。">地域内順位</span>
      需要と負荷は<b>順位であって量ではありません</b>。
      「需要 0.99」は「9,507 区画の中で上位 1%」という意味で、
      当事者が何人いるかを表す数ではありません。
      <span class="scale-tag is-absolute" title="需要 × 負荷。順位化していない生値。">需要 × 負荷</span>
      優先度はこの 2 つを掛けた値です（順位化していません）。
    </p>`;
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
    // **光らせられる行はクリックできることを見せる。** 「事業所 60 件」と
    // 書いてあってもどこにあるか分からない、というのが元の指摘だった。
    box.innerHTML =
      "<h2>この区画の実数</h2>" +
      '<p class="factor-source" style="margin:-4px 0 8px">' +
      "下線のある行を選ぶと、数えた施設が<b>下に一覧で開き</b>、同時に地図にも出ます。" +
      "<b>一覧の行数と地図の点の数は、ここの件数と必ず一致します。</b></p>" +
      sections
        .map(
          (sec) =>
            `<div class="fact-section">
               <div class="fact-section-title">${escapeHtml(sec.title)}</div>
               <div class="factor-source">${escapeHtml(sec.note)}</div>
               <table class="data-table">` +
            sec.items
              .map((f) => {
                const on = f.hl && state.highlight === f.hl.kind;
                const attrs = f.hl
                  ? ` class="is-highlightable${on ? " is-on" : ""}" data-hl="${f.hl.kind}"` +
                    ` data-r="${f.hl.radiusM}" role="button" tabindex="0"` +
                    ` aria-expanded="${on ? "true" : "false"}"`
                  : "";
                return (
                  `<tr${attrs}><th style="text-transform:none;letter-spacing:0">${escapeHtml(f.label)}</th>` +
                  `<td class="num">${escapeHtml(f.value)}</td></tr>` +
                  // **開いた行の直下に一覧を差し込む。** 地図の点を押すのは
                  // 250m 四方に 60 件が重なる場所では現実的でなく、
                  // 「60 件」と書いてある行のすぐ下に名前が並ぶほうが早い。
                  (on ? facilityListRow(state, row) : "")
                );
              })
              .join("") +
            "</table></div>",
        )
        .join("");

    for (const tr of box.querySelectorAll<HTMLElement>("tr[data-hl]")) {
      const kind = tr.dataset.hl as HighlightKind;
      const fire = () => onHighlight(state.highlight === kind ? null : kind);
      tr.addEventListener("click", fire);
      tr.addEventListener("keydown", (e) => {
        if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") {
          e.preventDefault();
          fire();
        }
      });
    }

    // 一覧の項目を押したら、その施設へ地図を寄せる。
    // **一覧は開いた行の直下にある別の `<tr>` なので、行のクリック判定とは
    // 別物**（同じ `<tr>` に入れていたら、一覧を触るたびに一覧が閉じる）。
    for (const li of box.querySelectorAll<HTMLElement>(".fl-item[data-lon]")) {
      const lon = Number(li.dataset.lon);
      const lat = Number(li.dataset.lat);
      const fire = () => onFocusPoint(lon, lat);
      li.addEventListener("click", fire);
      li.addEventListener("keydown", (e) => {
        if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") {
          e.preventDefault();
          fire();
        }
      });
    }
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
      const tag = scaleTag(f.component);
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
    // **「測定点が疎だから」と書いていたが、それは確かめずに書いた理由だった。**
    // 2026-08-04 に出典リンクを総点検したところ、参照していたデータセット ID は
    // カタログに存在せず（404）、東京都環境局が騒音として公開しているのは
    // 自動車交通騒音調査結果（平成12〜25年度）の 14 件だけだった。
    // 疎密以前に、点データとして入手できていない。
    [
      "鉄道騒音は入っていない",
      "都の鉄道騒音・振動調査は報告書としては公表されているが、" +
        "測定点の点データがオープンデータとして配信されていない" +
        "（2026-08-04 時点でカタログに該当データセットが無い）。" +
        "線路の位置から距離減衰で推定することはできるが、" +
        "その妥当性を検証する実測が無いため入れていない。",
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
    // **「提言の単位は地区であって地点ではない」と書いていた。**
    // 地区は 2026-08-03 に廃止したのに、この 1 項目と凡例の 2 箇所だけ
    // 文言が残り、**画面にしか存在しない概念**を主張し続けていた
    // （2026-08-04 に外部から「地区とは結局なんなのか」と指摘されて発覚）。
    // 束ねるのをやめた理由——どこまで束ねるかに根拠が無い——を項目にする。
    [
      "区画の中のどこに置くべきかは言えない",
      `示すのは 250m 四方の区画そのもので、その中の地点ではない。` +
        "かつては上位区画を隣接関係で束ねて「○○周辺」という地区を提言の" +
        "単位にしていたが、束ね方（格子で接している）に根拠はあっても" +
        "**どこまで束ねるかには無かった**ためやめた——地区の広がりは" +
        "「上位何件を束ねるか」で決まり、優先度は連続していてそこに" +
        "切れ目が無い。いま隣接は、単位ではなく「表示中の上位何件のうち" +
        "何区画が接しているか」という記述としてだけ使っている。",
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
  //
  // **「その区画が属する地区」と書いていた。** 地区は 2026-08-03 に
  // 廃止した概念で、画面のここだけに生き残っていた。**破線が囲むものは
  // 変わっていない**（選択中の区画と格子で接している上位区画）——
  // 変わったのは、それを「地区」という単位として述べるのをやめたこと。
  // 記述として言い直す。
  document.getElementById("legend-note")!.innerHTML =
    `${escapeHtml(meta.mesh_label)}・${meta.mesh_count.toLocaleString("ja-JP")}区画。` +
    "<br>実線 = 選択中の区画 / 破線 = それと接している上位区画。";
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
 * 尺度の札。**「対象地域を変えれば値も変わる」だけでは伝わらない。**
 *
 * 何と比べているのか（母数）と、変えるとはどういうことか（多摩地域まで
 * 広げて計算し直す、など）を具体で書く。母数は層ごとに違う——値 0 は
 * 「存在しない」として厳密に 0 に固定し、正の値を持つ区画の中だけで
 * 順位を付けているため（特別支援学校 3,626 / 障害福祉事業所 9,168）。
 */
function scaleTag(c: ComponentDef): string {
  if (c.absolute) {
    return (
      `<span class="scale-tag is-absolute" title="${escapeAttr(
        "法令が決めた物差しなので、比べる相手に左右されません。" +
          `${c.absolute.label}。根拠: ${c.absolute.basis}`,
      )}">絶対尺度</span>`
    );
  }
  const n = c.rank_denominator;
  const label = n ? `地域内順位（${num(n)}区画中）` : "地域内順位";
  return `<span class="scale-tag" title="${escapeAttr(
    "順位なので、比べる相手が変わると値も変わります。" +
      (n ? `いま比べているのは、この層の値が 0 でない ${num(n)} 区画。` : "") +
      "たとえば多摩地域まで広げて計算し直すと、同じ区画でも値が変わります。",
  )}">${escapeHtml(label)}</span>`;
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
      scaleTag(c) +
      `<span class="factor-source">${escapeHtml(c.absolute.label)}` +
      `（根拠: ${escapeHtml(c.absolute.basis)}）。` +
      "<b>比べる相手に左右されません</b>——多摩地域まで広げて計算し直しても、" +
      "大阪で計算しても、同じ場所は同じ値です。</span>"
    );
  }
  const n = c.rank_denominator;
  return (
    scaleTag(c) +
    '<span class="factor-source">' +
    (n
      ? `この層の値が 0 でない ${num(n)} 区画の中で、何番目かを 0〜1 に配分。`
      : "対象地域内での順位を 0〜1 に配分。") +
    "<b>比べる相手が変われば、値も変わります</b>——" +
    "たとえば多摩地域まで広げて計算し直すと、同じ区画でも違う値になります。</span>"
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
      <b>比べる相手が変われば値も変わります</b>（下で説明します）。残る
      <b>${absolute.length} 層</b>（${absolute.map((c) => escapeHtml(c.label.split("（")[0])).join("・")}）は
      法令・告示に 0 と 1 を固定した<b>絶対尺度</b>で、対象地域を変えても値は変わりません。
    </p>
    <p class="card-narrative">
      <b>「比べる相手が変わる」とは。</b>この地図はいま東京 23 区の ${n} 区画で
      計算しています。順位の層は「その中で何番目か」なので、
      <b>もし多摩地域まで広げて計算し直せば、同じ区画でも値が変わります</b>
      （周りの顔ぶれが変わるため）。絶対尺度の層は変わりません——
      騒音 70dB は 23 区で計算しても多摩を入れて計算しても 0.75 のままです。
    </p>
    <p class="card-narrative">
      <b>順位の母数は層ごとに違います。</b>値 0 は「そこに無い」として厳密に 0 に
      固定し、<b>正の値を持つ区画の中だけで</b>順位を付けているためです。
      たとえば特別支援学校は ${meta.components.find((c) => c.key === "sped_school")?.rank_denominator?.toLocaleString("ja-JP") ?? "—"} 区画、
      障害福祉サービス事業所は ${meta.components.find((c) => c.key === "welfare_capacity")?.rank_denominator?.toLocaleString("ja-JP") ?? "—"} 区画が母数で、
      「特別支援学校 0.5」は ${n} 区画の真ん中ではなく
      <b>「学校の徒歩圏に入っている区画の中で真ん中」</b>という意味です。
    </p>
    <p class="card-narrative">
      <b>層を重み付きで足したあと、需要スコアと負荷スコアにもう一度この順位化を
      掛けています。</b>したがって「需要 0.99」は<b>順位であって量ではありません</b>
      （9,507 区画の中で上位 1%、という意味）。重みの合計が変わっても色のスケールが
      動かないようにするための処理ですが、分布の広さを捨てているのは各層と同じです。
      <b>優先度だけは順位化していません</b>——需要 × 負荷の生値をそのまま出しています
      （2026-08-03 に 3 回目の順位化を除去。順位は 1 つも動いていません）。
    </p>
    <p class="card-narrative">
      <b>絶対尺度の「変わらない」は、最終スコアまでは残りません。</b>
      層の値を足したあとに順位化しているので、画面に出る需要・負荷の 2 値は
      対象地域に依存します。絶対尺度が保証しているのは
      <b>「その層が足し算に持ち込む値」まで</b>です。
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
