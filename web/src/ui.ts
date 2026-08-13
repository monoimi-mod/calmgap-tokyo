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
  tab: "ranking" | "selected" | "layers";
  /**
   * 「レイヤー別」タブでいま見ている層。
   *
   * **合成後の順位表だけでは、どの層が何を言っているのかが見えない。**
   * 優先度は 8 層を重み付きで足して掛けた値なので、「駅の乗降規模が
   * 大きいのはどこか」を画面から知る方法が無かった（内訳は選んだ 1 区画に
   * ついてしか出ない）。層ごとの上位を出すと、**その層が何を拾う層なのかが
   * 一覧で分かる**——同時に、上位が層ごとに全然違うことも見える。
   */
  layerKey: string;
  activePreset: string;
  /** 地図に塗る値。順位は常に優先度で決まる。 */
  displayMode: "priority" | "demand" | "load";
  /**
   * 順位表に出す件数。**選べるようにしてあるのは、打ち切りに根拠が
   * 無いことを隠さないため**（優先度は連続していて、どこにも切れ目が無い）。
   */
  rankingN: number;
  /**
   * 順位表の絞り込み。**別のタブにはしない。**
   *
   * 「優先度の順位」と「既存施設で届いていない区画」は、この作品の
   * 2 つの出力でありながら**別の場所を指している**——上位 50 区画のうち
   * 到達不可は 0 件で、到達不可 1,058 区画の順位の中央値は 8,083 位である。
   * 別々の画面に置くと「結局どちらを見ればよいのか」で終わるが、
   * **同じ一覧の切り替えにすると、押した瞬間に上位が消えること自体が
   * 「重みで決まる順位と、重みでほぼ決まらない不足は別物だ」という
   * 観察になる。**
   */
  rankingFilter: "all" | "unreachable";
  /**
   * 「区内で優先度が中位以上の到達不可区画」の添字（`midOrAboveSet` の出力）。
   *
   * **1 回数えて、見出し数値・順位表・地図の 3 箇所へ同じものを配る。**
   * 3 者が別々に数えると、同じ規則で数えているつもりのまま食い違い得る
   * （「表の件数・地図の点・一覧の行数」で既に一度やっている）。
   * 重みで動くのでスコアを計算し直すたびに作り直す。
   */
  midOrAbove: Set<number>;
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
  /**
   * 層ごとの、区画の順位。**重みに依存しないので 1 回だけ作る。**
   * 起動時に `computeLayerRanks` が作り、以後変わらない。
   */
  layerRanks: Record<string, LayerRank>;
}

/** 1 層ぶんの順位。`computeLayerRanks` の出力。 */
export interface LayerRank {
  /**
   * 区画ごとの順位（1 始まり・値の降順）。同値は同順位。
   * **0 は「順位が無い」**——その層の値が 0 の区画で、
   * 順位を付ける母集団に入っていない（zeroIsAbsence の層）。
   */
  rank: Int32Array;
  /** 母数。順位を付けた区画の数。 */
  denom: number;
  /** 値の降順に並べた区画の添字。レイヤー別タブの表に使う。 */
  order: number[];
}

/**
 * 層ごとの順位を作る。
 *
 * **画面は「地域内順位」という札を出しながら、順位そのものを出していなかった。**
 * 母数（3,626 区画中）は書いてあるのに何番目かが無く、代わりに 0〜1 の
 * 正規化値だけが出ていた——0.995 が何位なのかは、母数を掛け算すれば
 * 出せるが、読む側にそれをさせていた。
 *
 * **順位は配信された `n_*` から作る。** 別の値から作ると、札の言う順位と
 * 画面の値が食い違い得る。同値は同順位（1,1,3。競技順位）。
 *
 * 絶対尺度の層（騒音・用途地域）にも順位は付ける——**ただし画面では
 * 「参考」として扱う。** その層の値を決めているのは順位ではなく法令の物差しで、
 * 順位はあくまで「23 区の中でどのあたりか」を言うだけである。
 */
export function computeLayerRanks(
  rows: MeshProps[],
  components: ComponentDef[],
): Record<string, LayerRank> {
  const out: Record<string, LayerRank> = {};
  for (const c of components) {
    const field = `n_${c.key}`;
    const values = rows.map((r) => (r[field] as number) ?? 0);
    // 順位を付ける母集団。値 0 が「存在しない」層では、0 の区画を外す
    //（母数が層ごとに違うのはこのため。etl/build.py の rank_denominator）。
    const pool =
      c.zeroIsAbsence && !c.absolute
        ? values.map((v, i) => (v > 0 ? i : -1)).filter((i) => i >= 0)
        : values.map((_, i) => i);
    pool.sort((a, b) => values[b] - values[a]);

    const rank = new Int32Array(rows.length);
    let k = 0;
    while (k < pool.length) {
      let j = k;
      while (j + 1 < pool.length && values[pool[j + 1]] === values[pool[k]]) j++;
      for (let m = k; m <= j; m++) rank[pool[m]] = k + 1;
      k = j + 1;
    }
    // 配信された母数と食い違ったら、画面のどこかが古い。
    // 地図ごと止めるほどではないので、コンソールに出して先へ進む。
    if (c.rank_denominator != null && c.rank_denominator !== pool.length) {
      console.warn(
        `[layer-rank] ${c.key}: 母数が meta と食い違う ` +
          `(meta ${c.rank_denominator} / 実データ ${pool.length})`,
      );
    }
    out[c.key] = { rank, denom: pool.length, order: pool };
  }
  return out;
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
  | "station_nearest"
  // **騒音だけは「徒歩圏に在るもの」ではない。** 光らせるのは施設ではなく
  // 測定地点で、半径も帯域ではなく IDW の打ち切り距離（1,500m）。
  // 0 件ならその区画の騒音は測定値ではなく 23 区の中央値である。
  | "noise"
  // **公園は「徒歩圏に在るもの」でも「測ったもの」でもない。**
  // 光らせるのは**この区画に重なる公園**で、半径では選ばない
  // （被覆率がそういう数え方だから。`etl/build.py` の green_parks）。
  // 選ぶのはブラウザではなく Python で、配信された `gp` を引くだけ。
  | "green";

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
/**
 * 「区内で優先度が中位以上の到達不可区画」の添字。**ブラウザ側で数え直す。**
 *
 * `etl/hosts.py` の `reach_report` と同じ規則:
 *   到達不可 = 徒歩圏（700m）に区の公共施設が 1 件も無い（`f_host_n == 0`）
 *   しきい値 = **区内の優先度の中央値**（境界を含む）。母数は区内の全区画
 *   区の判定が付かなかった区画（水面など）は数えない
 *
 * **なぜ meta の固定値を使わないのか。** `meta.unreachable.mid_or_above` は
 * **既定の重みで 1 回だけ計算した値**である。しきい値は区内の優先度の
 * 中央値なので、**重みを動かせばこの集合は動く**——にもかかわらず画面は
 * 「この数字は重みにもスコアにも依存しません」と書いていた。
 * 動かなかったのは依存していないからではなく、**配信時に凍らせた数を
 * そのまま出していたから**である。
 *
 * 依存の度合いは小さい（到達不可の判定そのものは重みを通らず、重みが効くのは
 * 区内中央値との大小だけ）。**小さいことと、無いことは違う。** 数え直せば
 * 画面の数と一覧の行数と地図の区画が同じ規則から出た同じものになり、
 * 実際にどれだけ動くのかも見える。
 *
 * **既定の重みでは `meta.unreachable.mid_or_above` と一致しなければならない。**
 * 食い違うなら、Python とここのどちらかが上の規則から外れている。
 */
export function midOrAboveSet(state: AppState): Set<number> {
  const { rows, score } = state;

  // 区ごとに優先度を集めて中央値を出す。母数は区内の全区画。
  const byWard = new Map<number, number[]>();
  for (let i = 0; i < rows.length; i++) {
    const w = rows[i].w;
    if (typeof w !== "number") continue;
    const bucket = byWard.get(w);
    if (bucket) bucket.push(score.priority[i]);
    else byWard.set(w, [score.priority[i]]);
  }

  const median = new Map<number, number>();
  for (const [w, values] of byWard) {
    values.sort((a, b) => a - b);
    const n = values.length;
    // 偶数個なら中央 2 つの平均（pandas の median と同じ）。
    median.set(
      w,
      n % 2 ? values[(n - 1) / 2] : (values[n / 2 - 1] + values[n / 2]) / 2,
    );
  }

  const out = new Set<number>();
  for (let i = 0; i < rows.length; i++) {
    const w = rows[i].w;
    if (typeof w !== "number") continue;
    if (((rows[i].f_host_n as number) ?? 0) !== 0) continue;
    if (score.priority[i] >= (median.get(w) ?? Infinity)) out.add(i);
  }
  return out;
}

export function rankingRows(state: AppState): RankRow[] {
  const { rows, score, meta } = state;

  // 絞り込みは母数を差し替えるだけ。**並べ方も打ち切りも変えない**
  // ——同じ規則で並べた同じ順位表の、見る範囲が違うだけである。
  const pool =
    state.rankingFilter === "unreachable"
      ? score.order.filter((i) => state.midOrAbove.has(i))
      : score.order;
  const top = pool.slice(0, state.rankingN);
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
 * 1 レイヤーぶんの「実数」。ETL が f_* として配信している表示専用の値。
 *
 * **かつては「この区画の実数」という独立した節だった。** 8 層の正規化値を
 * 並べる「需要側の内訳／負荷側の内訳」とは別の場所に、同じ 8 層の実数が
 * 別の順序で並んでいた——**同じレイヤーが画面の 2 箇所に、対応の付かない
 * 形で出ていた**（「障害福祉サービス事業所 60件」と「障害福祉サービス
 * 事業所 0.995」が離れた場所にあり、どちらがどちらの根拠なのか読めない）。
 * 実数を内訳の行そのものへ畳んだので、この関数は 1 行ぶんを返す。
 */
export interface LayerFact {
  /** 実数の文字列。空文字なら、その層に実数が無い。 */
  value: string;
  /** 何を数えた値かの一言。半径がバラバラな理由がここで分かる。 */
  basis: string;
  /** 地図に光らせられる層。数えた半径も持つ（円を描くため）。 */
  hl?: { kind: HighlightKind; radiusM: number };
}

/**
 * 構成要素のキー → `meta.fact_radius_m` のキー。
 *
 * **半径は層ごとに違う値で、しかも意味も違う**（需要 3 層と駅は帯域、
 * 騒音は内挿の打ち切り）。対応表を 1 つ置いて、画面のどこでも同じ
 * 引き方をする。ここに無い層は、半径という概念を持たない層である。
 */
const RADIUS_KEY: Record<string, keyof Meta["fact_radius_m"]> = {
  welfare_capacity: "welfare",
  sped_school: "school",
  clinic: "clinic",
  station_flow: "station",
  noise: "noise",
};

export function layerFact(
  key: string,
  row: MeshProps,
  radius: Meta["fact_radius_m"],
): LayerFact {
  const g = (k: string) => row[k] as number | undefined;
  const s = (k: string) => row[k] as string | undefined;

  // **徒歩圏の半径は、その層がスコアに使っている帯域と同じ値。**
  // 層ごとに違うのはそのためで、表示のために選んだ数字ではない
  // （駅だけ 1,500m の最寄り 1 駅だった名残を 2026-08-04 に直した）。
  const walk = (r: number) => `徒歩圏 ${num(r)}m の件数`;

  switch (key) {
    case "welfare_capacity": {
      const n = g("f_welfare_n");
      if (!n) return { value: "0件", basis: walk(radius.welfare) };
      const cap = g("f_welfare_cap");
      // 徒歩圏は区界で切っていない（エッジ効果を避けるため入力を区界の
      // 外側 2km まで拾う設計）。**その事実を実数の隣に出す**——外周の
      // メッシュは「隣の市の事業所」で需要が決まっていることがある。
      const outside = g("f_welfare_outside_n") ?? 0;
      return {
        value:
          `${num(n)}件${cap ? `（定員 ${num(cap)}人）` : ""}` +
          (outside ? ` ※うち対象23区の外 ${num(outside)}件` : ""),
        basis: walk(radius.welfare),
        hl: { kind: "welfare", radiusM: radius.welfare },
      };
    }
    case "sped_school": {
      const n = g("f_school_n");
      return n
        ? {
            value: `${num(n)}校`,
            basis: walk(radius.school),
            hl: { kind: "school", radiusM: radius.school },
          }
        : { value: "0校", basis: walk(radius.school) };
    }
    case "station_flow": {
      const n = g("f_station_n");
      if (!n) return { value: "0駅", basis: walk(radius.station) };
      const sum = g("f_station_sum");
      return {
        value: `${num(n)}駅${sum ? `（乗降計 ${num(sum)}人/日）` : ""}`,
        basis: walk(radius.station),
        hl: { kind: "station", radiusM: radius.station },
      };
    }
    case "clinic": {
      const n = g("f_clinic_n");
      return n
        ? {
            value: `${num(n)}件`,
            basis: walk(radius.clinic),
            hl: { kind: "clinic", radiusM: radius.clinic },
          }
        : { value: "0件", basis: walk(radius.clinic) };
    }
    // 負荷側は 4 層とも「区画自身に与えられた値」で、周囲を数えたものではない。
    // 需要側と基準が違うことを、行ごとの basis で言う。
    case "zoning":
      return { value: s("f_zoning_name") ?? "—", basis: "この区画の値（面積最大の区分）" };
    case "noise": {
      const db = g("f_noise_db");
      // **測定点の件数を、値と同じ行に出す。** 0 件は「静か」ではなく
      // 「測っていない」——打ち切り距離の内に測定点が 1 つも無い区画では、
      // ここに出ている dB は測定でも内挿でもなく **23 区の中央値**である
      //（496 区画。docs/issues.md A6）。件数を書かないと、その 496 区画と
      // 実測 12 点から内挿した区画が、画面で同じ見た目になる。
      const n = g("f_noise_n") ?? 0;
      return {
        value:
          (db != null ? `${db} dB (LAeq)` : "—") +
          (n
            ? `・内挿に使った測定点 ${num(n)}点`
            : "・測定点なし（23 区の中央値で補完）"),
        basis:
          `この区画の値（半径 ${num(radius.noise)}m 以内の測定点からの距離重み付き内挿）。` +
          "この半径は徒歩圏ではなく、内挿の打ち切り距離です。" +
          "測定点は幹線道路の道路端にしかありません。",
        // 0 件でも押せるようにする。**「測定点が無い」ことこそ地図で
        // 見せるべき**で、押しても何も起きないと「まだ実装されていない」に見える
        //（円だけが描かれ、その中が空であることが見える）。
        hl: { kind: "noise", radiusM: radius.noise },
      };
    }
    case "crowding":
      return {
        value: g("f_crowding") != null ? `${num(g("f_crowding")!)}人` : "—",
        basis: "この区画の値（経済センサス 従業者数・500m メッシュ）",
      };
    case "green": {
      const pct = g("f_green_pct") ?? 0;
      const n = g("f_green_n") ?? 0;
      return {
        // **「0%（屋外に退避先なし）」と書いていた。撤回した。**
        // この層が見ているのは区画に重なる面積だけで、隣の区画の公園も、
        // そこへ行けるかどうかも測っていない。
        value: pct > 0 ? `${pct}%（${num(n)}件）` : "0%（重なる公園なし）",
        // **半径ではなく重なりで数える。** 需要 4 層は帯域と同じ半径の
        // 円で数えるが、この層は区画そのものの被覆率なので、
        // 数えるべきは「この区画に重なる公園」である。
        // **揃っていない理由をこの行の隣に書く**（CLAUDE.md）。
        basis: "この区画に重なる公園の面積割合（半径ではなく重なりで数える）",
        // 0 件でも押せるようにする——**円が 1 つも無いことが見えるのが、
        // この層でいちばん言いたいこと**（隣に公園があっても 0% になる）。
        hl: { kind: "green", radiusM: 0 },
      };
    }
    default:
      return { value: "", basis: "" };
  }
}

/**
 * 順位表の見出しに使う「この区画の呼び名」。**需要の実数ではない。**
 *
 * 半径 1,500m はこのためだけの距離で、スコアには入らない。
 * 帯域の 600m に揃えると 9,507 区画のうち 4,197 件（44.1%）が名前を失う。
 */
export function nearestStationFact(
  row: MeshProps,
  radius: Meta["fact_radius_m"],
): FactItem | null {
  const name = row.f_station_name as string | undefined;
  if (!name) return null;
  const r = row.f_station_riders as number | undefined;
  const d = row.f_station_dist as number | undefined;
  return {
    label: name,
    // 最寄り 1 駅なので円は描かない（半径 0 = 円なし）。
    hl: { kind: "station_nearest", radiusM: 0 },
    value:
      (r ? `乗降 ${num(r)}人/日` : "乗降規模不明") +
      (d != null ? ` / ${num(d)}m` : "") +
      `（${num(radius.station_max)}m 以内の最寄り 1 駅）`,
  };
}

/**
 * 供給側について言える唯一の実数。**スコアには一切入らない。**
 *
 * 700m だけ半径が違うのは、これが帯域ではなく
 * 「徒歩圏に 1 件も無い＝到達不可」という主張の定義そのものだから。
 * 数えているのは施設一覧の行数であって建物の数ではない（docs/issues.md）。
 */
export function hostFact(row: MeshProps, radius: Meta["fact_radius_m"]): FactItem {
  const n = (row.f_host_n as number | undefined) ?? 0;
  return {
    label: "区の公共施設",
    value: n ? `${num(n)}件（一覧の行数）` : "0件",
    ...(n ? { hl: { kind: "host" as const, radiusM: radius.host } } : {}),
  };
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

  // **かつてここは「屋外に代替の退避先が存在しない」と書いていた。誤りである。**
  // 見ているのはこの区画に重なる公園の面積割合だけで、隣の区画の公園も、
  // そこへ行けるかどうかも測っていない。元データは点で、面積の等しい
  // 円に置き換えてある（等価半径の中央値 18m）。Python 側（etl/hosts.py の
  // narrate）と同じ文にすること——2 つある根拠文が食い違うと、
  // 同じ区画が画面と JSON で別のことを言う。
  // 直前の loadBits が既に「緑・公園被覆なし」と言っているので、
  // ここで足すのは**その 0% が何を意味しないか**だけにする。
  if (green < 3) {
    parts.push("ただしこれは区画に重なる面積で、隣の区画にある公園は数えていない。");
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
/**
 * 画面の最上部に置く「結論」。**方法と結果を同じ塊に入れる。**
 *
 * ここが空いていたために、画面は**限界の説明から始まっていた**——
 * 感度分析・プリセット共通 0 件・特別支援学校依存・固定値 76 個が
 * 結論より上に並び、読み終えた人に残るのは「この数字は信じてはいけない」
 * だけだった。**限界は 1 文字も削っていない。下の折りたたみへ移した。**
 *
 * **2 つ出すのは、この作品の出力が 2 つあるからである。**
 * 片方（順位）は重みで動き、もう片方（到達不可）は重みがほとんど入らない。
 * **どちらか一方だけを結論として出すと、もう一方が付け足しに見える**——
 * 実際、以前は 148 だけが大きく出ていて、順位表は右のタブの中にあった。
 *
 * **「◯◯周辺」とは書かない**（2026-08-03 に地区をやめた）。
 * 区名と最寄り駅名は**区画の呼び名**であって、広がりの主張ではない。
 */
export function renderFindings(
  state: AppState,
  onShowRanking: () => void,
  onShowUnreachable: () => void,
): void {
  const el = document.getElementById("findings");
  if (!el) return;
  const { meta, rows, score } = state;

  const topIdx = score.order[0];
  const topRow = topIdx != null ? rows[topIdx] : undefined;
  const where = topRow
    ? [
        typeof topRow.w === "number" ? (meta.target_wards[topRow.w] ?? "") : "",
        (topRow.f_station_name as string) ?? "",
      ]
        .filter(Boolean)
        .join(" ")
    : "";

  el.innerHTML = "";

  const card = (
    kicker: string,
    value: string,
    unit: string,
    note: string,
    action: string,
    onClick: () => void,
  ): void => {
    const d = document.createElement("div");
    d.className = "finding";
    d.innerHTML =
      `<div class="finding-kicker">${kicker}</div>` +
      `<div class="finding-value">${value}<small>${unit}</small></div>` +
      `<div class="finding-note">${note}</div>`;
    const b = document.createElement("button");
    b.className = "finding-btn";
    b.type = "button";
    b.textContent = action;
    b.addEventListener("click", onClick);
    d.appendChild(b);
    el.appendChild(d);
  };

  // **母数を隣に置く。** 「50 区画」だけだと表示件数の設定に見えるが、
  // 「9,507 のうち 50」と書けば**絞り込みそのものが結論である**ことが伝わる
  //（この道具の自己説明は「9,507 区画を人が話し合える数まで絞る」である）。
  card(
    "需要 × 負荷がともに高い区画",
    num(state.rankingN),
    ` / ${num(meta.mesh_count)} 区画`,
    // **打ち切りに根拠が無いことを、結論の隣で言う。** 順位表の中にも
    // 書いてあるが、ここで数を大きく出す以上、ここでも言う必要がある。
    `優先度の高い順に出しています（件数は選べます）。` +
      (where ? `いま 1 位は <b>${escapeHtml(where)}</b>。` : "") +
      `<b>この打ち切りに根拠はありません</b>——優先度は連続していて切れ目がありません。`,
    "順位表を見る",
    onShowRanking,
  );

  card(
    `需要が高いのに、徒歩圏（半径 ${num(meta.host_max_distance_m)}m）に区の公共施設が 1 件も無い区画`,
    num(state.midOrAbove.size),
    ` / ${num(meta.mesh_count)} 区画`,
    // **「退避先が無い」とは書かない。** 数えているのは区の公共施設
    // （図書館・出張所・児童館）で、そこにカームダウンスペースが
    // あるわけではない——外部照合では、23 区の図書館に 1 館も無い。
    // **施設があっても退避先があることにはならない**ので、この数が
    // 言えるのは「転用しうる屋内の公共空間が近くに無い」までである。
    "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要ります。" +
      "<b>公共施設があっても、そこにカームダウンスペースがあるとは限りません</b>" +
      "（数えているのは転用しうる場所の有無です）。" +
      "<b>重みでも動きます</b>（下の折りたたみ）。",
    "絞り込んで地図に出す",
    onShowUnreachable,
  );

  // --- 外部照合 ---
  //
  // **この作品で唯一、外の物差しで確かめた部分である。** parity も selftest も
  // 内部整合しか見ておらず、**モデルが現実を当てている証拠にはならない。**
  //
  // **数字は meta から入れる**（順位はビルドごとに動く。ここに書けば必ず古くなる）。
  const cs = meta.calm_spaces;
  if (cs && cs.rank_median != null) {
    const p = document.createElement("p");
    p.className = "findings-check";
    p.innerHTML =
      `<b>外部照合。</b>東京 23 区で既に置かれているカームダウンスペースは、` +
      `調べた範囲で <b>${num(cs.site_count)} か所・${num(cs.room_count)} 室</b>。` +
      `その区画の優先度順位は <b>中央値 ${num(cs.rank_median)} 位</b>` +
      `（${num(cs.mesh_count)} 区画中）で、<b>上位 50 に入るものは ${cs.in_top_50} 件</b>、` +
      `${cs.in_bottom_half} 件は下位半分にあります。` +
      `<br><br>` +
      // **室数だけを数えてはいけない。** 「16 室ある」と読めてしまうが、
      // その大半は施設の中にあり、その施設の利用者しか使えない。
      // **この地図が前提にしているのは「街を歩いている人が使えるか」**である。
      `そのうち<b>街を歩いている人がその場で使えるのは ${cs.open_rooms} 室</b>` +
      `——残りは大学の関係者のみ（${cs.rooms_by_access.members ?? 0} 室）、` +
      `保安検査後（${cs.rooms_by_access.airside ?? 0} 室）、` +
      `入館料やチケットが要るもの（${cs.rooms_by_access.ticketed ?? 0} 室）です。` +
      `<br><br><b>いま置かれている場所と、この地図が指す場所は一致していません。</b>` +
      `既存の設置は施設ごとの判断で、都市全体の需給から決まってはいないためです。` +
      `<span class="findings-check-note">手で集めた一覧との照合（${escapeHtml(cs.surveyed_at)} 時点）。` +
      `中央集約されたオープンデータが無いため<b>網羅性の保証はありません</b>。スコアには入っていません。</span>`;
    el.appendChild(p);
  }
}

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
  // **格下げであって撤去ではない。** 1,058 は補足として残す。
  //
  // **2026-08-06: この節は折りたたみの中へ移した。** 数そのものは
  // 画面最上部の結論（`renderFindings`）が出しており、ここに残るのは
  // **その数が何に寄りかかっているか**の説明である。見出しもそう書く。
  kickerEl.textContent =
    "上の 2 つ目の数（区内で優先度が中位以上、かつ" +
    `徒歩圏 ${num(meta.host_max_distance_m)}m に区の公共施設が 1 件も無い区画）について`;

  // **配信された固定値ではなく、いまの重みで数え直した値を出す。**
  // meta.unreachable.mid_or_above は既定の重みで 1 回計算した数で、
  // しきい値が区内の優先度の中央値である以上、**重みを動かせば動く**。
  // 動かないように見えていたのは凍らせた数を出していたからにすぎない。
  const midN = state.midOrAbove.size;

  valueEl.innerHTML = `${midN.toLocaleString("ja-JP")}<small> 区画</small>`;

  // **かつてここには「この数字は重みにもスコアにも依存しません
  // （スライダーを動かしても変わりません）」と書いてあった。誤りである。**
  //
  // 到達不可の判定（徒歩圏に 1 件も無い）自体は確かに重みを通らない。
  // だがこの見出し数値はそこへ**「区内で優先度が中位以上」**という条件を
  // 重ねたもので、優先度は重みで動く。プリセットを替えるだけで
  // 133〜171 件に動き、**顔ぶれは既定と 107〜139 件しか共通しない**
  //（重み ±30% を 200 回振ると 119〜187 件）。
  //
  // **動かないように見えていたのは、配信時に凍らせた数を出していたから
  // だけである。** 数え直すようにしたので、いまはスライダーで動く。
  //
  // **範囲をここに書かない。** 書けば必ず古くなる（この作品が繰り返し
  // 踏んできた型）。動く様子はスライダーを動かせば画面が見せる。
  labelEl.innerHTML =
    "既存ストックの徒歩圏から外れており、新規整備か民間施設との連携が要る区画です。" +
    `<br><br><b>この件数は重みで動きます。</b>` +
    "「徒歩圏に 1 件も無い」という判定そのものに重みは入りませんが、" +
    "そこへ<b>「区内で優先度が中位以上」</b>という条件を重ねているためです" +
    "（スライダーを動かすと、この数字も動きます）。" +
    "<br><br><b>さらに、方法そのものに寄りかかっています。</b>" +
    "何を需要と見なすか、何を退避先と見なすか、" +
    `徒歩 ${num(meta.host_max_distance_m)}m という距離、` +
    "「区内で中位以上」という線引き——この 4 つはこちらが決めたもので、" +
    "変えれば件数も顔ぶれも変わります。" +
    "<b>言えるのは「この方法の内側での結果」までです。</b>" +
    `<br><br>しきい値は区ごとの優先度の中央値で、母数は区内の全区画です。` +
    `<br><br>徒歩圏に公共施設が無い区画は、全体では ` +
    `<b>${u.count.toLocaleString("ja-JP")} / ${meta.mesh_count.toLocaleString("ja-JP")} 区画` +
    `（${Math.round(u.ratio * 1000) / 10}%）</b>。` +
    "ただし<b>その大半は優先度の下位</b>で、皇居・羽田空港・埋立地・河川敷など" +
    "人のいない場所が主成分です。" +
    "<b>区ごとにこの割合を出して並べてはいけません</b>——" +
    "非市街地の面積比でほぼ決まってしまいます。" +
    `<br><br>現在の重みでの上位 ${N} 区画のうち、公共施設が徒歩圏に無いものは ${uncoveredTop} 件。` +
    '<br><span class="stat-hint">' +
    "順位表の「表示」を切り替えると、この " +
    `${midN.toLocaleString("ja-JP")} 区画を優先度順に並べて出します（地図にも重なります）。` +
    "</span>";
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

/**
 * 「8 つのレイヤー」と「実データ 10/10 レイヤー」の対応を画面で解く。
 *
 * **どちらも正しいのに、対応がどこにも書かれていなかった**
 *（2026-08-05・外部から「8 なの 10 なの、意味が分からない」と指摘されて発覚）。
 * 片方の数字を書き換えて食い違いを消すのではない——
 * **2 層がスコアに入らないという事実そのものが答え**である。
 * しかもその 1 つ（既存の公共施設）は、この作品でいちばん頑健な出力
 *「到達不可」を作っている層で、**スコアに入らないことが長所**になっている。
 *
 * 数はすべて meta から出す（ここに書くと、層が増えたときに古くなる。
 * 分類漏れは etl/build.py の `_layer_roles` が止める）。
 */
export function renderLayerRoles(meta: Meta): void {
  const scored = meta.layer_roles.filter((r) => r.role === "score");
  const support = meta.layer_roles.filter((r) => r.role === "support");

  const heading = document.getElementById("layer-heading");
  if (heading) {
    heading.textContent = `評価に使う ${scored.length} つのデータ（レイヤー）`;
  }

  const el = document.getElementById("layer-roles-note");
  if (!el) return;
  el.innerHTML =
    `この ${scored.length} 層を重み付きで足し合わせて需要スコアと負荷スコアを作り、` +
    "2 つを掛けて優先度にしています。重みを動かすと地図も提言も変わります。" +
    "<br><br>" +
    `<b>データの層は全部で ${meta.layer_roles.length} 層あります。</b>` +
    `そのうち<b>スコアに入るのが ${scored.length} 層</b>（下のスライダー）、` +
    `<b>入らないのが ${support.length} 層</b>です。` +
    "<ul class='layer-role-list'>" +
    support
      .map(
        (r) =>
          `<li><b>${escapeHtml(r.label)}</b>（<code>${escapeHtml(r.layer)}</code>）<br>` +
          `${boldMd(r.note)}</li>`,
      )
      .join("") +
    "</ul>" +
    `「実データ ${meta.real_layer_count}/${meta.layer_total} レイヤー」は` +
    `この ${meta.layer_roles.length} 層のことで、スライダーの ${scored.length} 本とは` +
    "数え方が違うだけです。";
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
          <label for="${id}" title="${escapeAttr(plainMd(c.rationale))}&#10;&#10;出典: ${escapeAttr(plainMd(c.source))}">${escapeHtml(c.label)} ${tag}</label>
          <span class="slider-value" id="${id}-val">${fmt(weights[c.key] ?? c.weight, 1)}</span>
        </div>
        <input type="range" id="${id}" min="0" max="2" step="0.1"
               value="${weights[c.key] ?? c.weight}"
               aria-label="${escapeAttr(c.label)} の重み" />
        <div class="factor-source slider-source">出典: ${boldMd(c.source)}</div>`;
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

/**
 * 順位表の絞り込み。**タブを増やさず、同じ一覧の切り替えにする。**
 *
 * 押すと上位がごっそり消える（既定の重みでは上位 50 区画の到達不可は 0 件）。
 * **その空振りが見えることに意味がある**——重みで決まる順位と、
 * 重みでほぼ決まらない不足は、別の場所を指しているという観察そのものだから。
 * 別タブにすると 2 つの独立した機能に見え、この対比が消える。
 */
export function renderRankingFilter(
  state: AppState,
  onPick: (id: AppState["rankingFilter"]) => void,
): void {
  const host = document.getElementById("ranking-filter");
  if (!host) return;
  host.innerHTML = '<span class="ranking-n-label">表示</span>';
  const options: { id: AppState["rankingFilter"]; label: string }[] = [
    { id: "all", label: "すべての区画" },
    {
      id: "unreachable",
      // **「退避先が無い」と書いてはいけない。** hosts は区の公共施設
      // （図書館・出張所・児童館）で、**そこにカームダウンスペースが
      // あるわけではない**。外部照合で、23 区の図書館には 1 館も
      // 設置されていないことが分かっている（`docs/status.md`）。
      // 数えているのは「転用しうる屋内の公共空間が近くにあるか」までである。
      label: `徒歩圏に区の公共施設が無い区画だけ（${num(state.midOrAbove.size)}）`,
    },
  ];
  for (const o of options) {
    const b = document.createElement("button");
    b.className = "preset-btn";
    b.type = "button";
    b.textContent = o.label;
    b.setAttribute("aria-pressed", String(o.id === state.rankingFilter));
    b.addEventListener("click", () => onPick(o.id));
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
  // エスケープしてから太字だけ戻す。**同じ処理をここに手書きしていた**
  // ——2 つあると片方だけ直せてしまうので boldMd に寄せる。
  const note = boldMd(preset.note);

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
  onFocusPoint: (index: number) => void = () => {},
  onPickLayer: (key: string) => void = () => {},
): void {
  const body = document.getElementById("detail-body")!;
  const pane = document.getElementById("detail")!;

  // **タブごとにスクロール位置を覚える。** 行を押すと「選択中の区画」へ
  // 移るようにしたので、戻ったときに一覧の頭へ飛ばされると、
  // **40 行目を調べていた人は毎回 40 行スクロールし直すことになる。**
  // 順位表は既定 50 行あり、そこを往復するのがこの画面の主な使い方である。
  if (lastTab && lastTab !== state.tab) scrollMemo[lastTab] = pane.scrollTop;

  body.innerHTML = "";

  if (state.tab === "ranking") renderRanking(body, state, onPick);
  else if (state.tab === "layers") renderLayerRanking(body, state, onPick, onPickLayer);
  else renderSelected(body, state, onHighlight, onFocusPoint);

  if (lastTab !== state.tab) pane.scrollTop = scrollMemo[state.tab] ?? 0;
  lastTab = state.tab;
}

/** タブごとの直近スクロール位置。再描画のたびに頭へ戻さないため。 */
const scrollMemo: Partial<Record<AppState["tab"], number>> = {};
let lastTab: AppState["tab"] | null = null;

/**
 * レイヤー別の上位区画。**合成後の順位表からは見えないものを出す。**
 *
 * 優先度は 8 層を重み付きで足して掛けた値なので、
 * 「駅の乗降規模が大きいのはどこか」を画面から知る方法が無かった
 *（内訳は選んだ 1 区画についてしか出ない）。層ごとの上位を並べると、
 * **その層が何を拾っているのかが一覧で分かる**——同時に、
 * **層ごとに上位の顔ぶれが全く違うこと**も見える。
 *
 * **並びはスコアに入る正規化値の順で、実数の順ではない。**
 * 需要側の 4 層は距離で重み付けして足しており（帯域のガウス核・
 * 打ち切りは帯域の 2 倍）、**実数は帯域と同じ半径での単純な件数**である。
 * 同じ 3 駅でも、駅が区画の真上にあるか帯域の縁にあるかで正規化値は違う。
 * その食い違いを隠さないために、両方を並べて出す。
 */
function renderLayerRanking(
  body: HTMLElement,
  state: AppState,
  onPick: (meshCode: string) => void,
  onPickLayer: (key: string) => void,
): void {
  const { meta, rows, score } = state;
  const comp =
    meta.components.find((c) => c.key === state.layerKey) ?? meta.components[0];
  const lr = state.layerRanks[comp.key];

  const intro = document.createElement("p");
  intro.className = "card-narrative";
  intro.style.marginBottom = "10px";
  intro.innerHTML =
    "<b>1 つの層だけで見た上位区画です。</b>優先度（8 層の合成）ではありません。" +
    "層を選ぶと、その層の値が大きい順に並びます。行を選ぶと地図がその区画へ寄ります。";
  body.appendChild(intro);

  // 層の選択。**並び順は config.py の定義順**（スライダー・内訳と同じ）。
  const picker = document.createElement("div");
  picker.className = "layer-picker";
  for (const side of ["demand", "load"] as const) {
    const group = document.createElement("div");
    group.className = "layer-picker-group";
    group.innerHTML = `<span class="layer-picker-label">${
      side === "demand" ? "需要側" : "負荷側"
    }</span>`;
    for (const c of meta.components.filter((x) => x.side === side)) {
      const b = document.createElement("button");
      b.className = "preset-btn";
      b.type = "button";
      b.textContent = shortLabel(c);
      b.setAttribute("aria-pressed", String(c.key === comp.key));
      b.addEventListener("click", () => onPickLayer(c.key));
      group.appendChild(b);
    }
    picker.appendChild(group);
  }
  body.appendChild(picker);

  const head = document.createElement("div");
  head.className = "layer-head";
  head.innerHTML =
    `<h3 style="margin:12px 0 4px">${escapeHtml(comp.label)} ${scaleTag(comp)}</h3>` +
    `<p class="factor-source">${boldMd(comp.rationale)}</p>` +
    `<p class="factor-source">出典: ${boldMd(comp.source)}</p>` +
    (comp.sign < 0
      ? '<p class="card-narrative"><b>この層は減点です。</b>' +
        "値が大きい区画ほど負荷スコアを<b>下げます</b>（既に安らげる場所として）。" +
        "下の表は値の大きい順なので、<b>優先度が高い順ではありません</b>。</p>"
      : "");
  body.appendChild(head);

  const N = Math.min(state.rankingN, lr.order.length);
  const table = document.createElement("table");
  table.className = "data-table is-ranking";
  table.innerHTML = `
    <thead>
      <tr>
        <th>この層<br>での順位</th><th>区 / 最寄り駅</th>
        <th>この区画の実数</th>
        <th class="num">正規化<br>値</th><th class="num">優先度<br>順位</th>
      </tr>
    </thead>`;
  const tbody = document.createElement("tbody");
  for (let k = 0; k < N; k++) {
    const i = lr.order[k];
    const row = rows[i];
    const fact = layerFact(comp.key, row, meta.fact_radius_m);
    const ward = typeof row.w === "number" ? (meta.target_wards[row.w] ?? "") : "";
    const station = (row.f_station_name as string) ?? "";
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    if (state.selected === row.c) tr.style.background = "var(--surface-2)";
    tr.innerHTML = `
      <td>${lr.rank[i]}</td>
      <td>${escapeHtml([ward, station].filter(Boolean).join(" ") || "—")}
        <span class="factor-source" style="font-family:var(--mono)">${escapeHtml(row.c)}</span></td>
      <td>${escapeHtml(fact.value)}</td>
      <!-- **ここだけ 4 桁で出す。** 3 桁だと上位が「1.000」で並び、
           順位が違うのに値が同じに見える（配信精度は 4 桁）。 -->
      <td class="num">${fmt((row[`n_${comp.key}`] as number) ?? 0, 4)}</td>
      <td class="num">${num(score.order.indexOf(i) + 1)}</td>`;
    tr.addEventListener("click", () => onPick(row.c));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);

  const note = document.createElement("p");
  note.className = "card-narrative";
  note.style.marginTop = "10px";
  // **「実数の順ではない」ことを表の下で言う。** 言わないと、
  // 実数が下の行より小さいのに上に来ている行が誤りに見える。
  note.innerHTML =
    "<b>並びは正規化値の順で、実数の順ではありません。</b>" +
    (comp.side === "demand"
      ? "需要側の 4 層は<b>距離で重み付けして足しています</b>" +
        `（帯域 ${num(meta.fact_radius_m[RADIUS_KEY[comp.key]])}m のガウス核）。` +
        "実数は同じ半径で数えた単純な件数なので、" +
        "<b>同じ件数でも、近くにあるか縁にあるかで正規化値は違います</b>。"
      : "負荷側は区画自身に与えられた値ですが、" +
        "正規化のしかたが層で違います（下の札）。") +
    "<br><br>" +
    (comp.absolute
      ? "<b>この層は絶対尺度です</b>——値を決めているのは順位ではなく" +
        `${escapeHtml(comp.absolute.label)}という物差しで、` +
        "この順位は「23 区の中でどのあたりか」を言うだけの<b>参考</b>です。"
      : `<b>母数は ${num(lr.denom)} 区画</b>——この層の値が 0 でない区画だけで` +
        `順位を付けています（全 ${num(meta.mesh_count)} 区画ではありません）。`) +
    "<br><br>" +
    "<b>この表は優先度ではありません。</b>右端の優先度順位を見ると、" +
    "この層で上位の区画が優先度でも上位とは限らないことが分かります——" +
    "優先度は需要と負荷の<b>掛け算</b>で、片側だけ極端な場所は上がりません。";
  body.appendChild(note);
}

function renderRanking(
  body: HTMLElement,
  state: AppState,
  onPick: (meshCode: string) => void,
): void {
  const { meta } = state;
  const list = rankingRows(state);
  const filtered = state.rankingFilter === "unreachable";
  const poolN = filtered ? state.midOrAbove.size : meta.mesh_count;

  const intro = document.createElement("p");
  intro.className = "card-narrative";
  intro.style.marginBottom = "10px";
  // **「提言リスト」と「表で見る」を分けていた理由はもう無い。**
  // 提言の単位を区画に戻した時点で、両者は同じ順位表の別表示になった。
  intro.innerHTML = filtered
    ? `<b>既存施設で届いていない区画だけ</b>を、同じ優先度の順に並べています。` +
      `徒歩圏（半径 ${num(meta.host_max_distance_m)}m）に区の公共施設が 1 件も無く、` +
      `かつ区内で優先度が中位以上の <b>${num(poolN)} 区画</b>のうち上位 ${list.length} 件。` +
      "<br><br>" +
      "<b>順位はこの " +
      `${num(poolN)} 区画の中での順位です</b>——全 ${meta.mesh_count.toLocaleString("ja-JP")} 区画の中での` +
      "順位ではありません（優先度の値がそれを示します）。"
    : `現在の重みでの優先順位です。<b>単位は 250m の区画</b>で、` +
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
    "<b>表示件数を変えればこの数も変わります</b> — 場所の性質ではありません。" +
    (filtered
      ? "<br><br>この絞り込みで残る区画は、<b>需要の定義・供給の定義・" +
        `徒歩 ${num(meta.host_max_distance_m)}m・「区内で中位以上」という 4 つの決めごとの上に立っています。</b>` +
        "どれを変えても件数も顔ぶれも変わります。" +
        "<b>重みでも動きます</b>——「中位以上」の判定に優先度を使っているためです" +
        "（スライダーで確かめられます）。" +
        "<br><br>なお、<b>重みを一切通らないのは「徒歩圏に 1 件も無い」という判定だけ</b>で、" +
        `そちらは ${meta.unreachable.count.toLocaleString("ja-JP")} 区画あります` +
        "——ただしその大半は人のいない土地です。"
      : "");
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
 * 開いている行の直下に差し込む施設一覧。
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
function facilityListBlock(state: AppState, row: MeshProps): string {
  const fc = state.highlightPoints;
  if (!fc) return "";
  // **0 件でも黙って閉じない。** 騒音の測定点は 496 区画で 0 件になり、
  // **そのことこそ見せるべき事実**である（issues.md A6）。何も出ないと
  // 「押しても動かない行」に見える。
  if (!fc.features.length) {
    // **緑・公園には円が無い。** 半径ではなく「区画に重なるか」で
    // 数えているので、「円の中が空です」と書くと存在しない距離を主張する。
    const empty =
      state.highlight === "green"
        ? "この区画に重なる公園は 1 件もありません。" +
          "<b>隣の区画にある公園は数えていません</b>——地図を少し引くと、" +
          "すぐ外に円があるかどうかが見えます。"
        : "この範囲には 1 件もありません（地図の円の中が空です）。";
    return `<div class="fact-list">
        <div class="fl-head is-empty">${empty}</div>
      </div>`;
  }

  const mx = row.mx as number | undefined;
  const my = row.my as number | undefined;

  // **添字を保ったまま距離順に並べる。** 押されたときに渡すのは座標ではなく
  // この添字で、`state.highlightPoints`（地図へ渡したのと同一の配列）を
  // 引き直す。同じ地点に 2 件あっても、一覧の行・地図の点・吹き出しの
  // 中身が同じ 1 件であることが構造的に保証される。
  const items = fc.features
    .map((f, index) => {
      const p = (f.properties ?? {}) as Record<string, unknown>;
      const x = p.x as number | undefined;
      const y = p.y as number | undefined;
      const d =
        typeof mx === "number" && typeof my === "number" &&
        typeof x === "number" && typeof y === "number"
          ? Math.hypot(x - mx, y - my)
          : null;
      const g = f.geometry;
      const hasPoint = !!g && g.type === "Point";
      return { p, d, index, hasPoint };
    })
    .sort((a, b) => (a.d ?? Infinity) - (b.d ?? Infinity));

  const rows = items
    .map(({ p, d, index, hasPoint }) => {
      // 騒音の測定点は施設ではないので、`capacity` の表とは別に書く。
      // 同じ「規模」の欄に dB を流し込むと、単位の違う 4 つ目の値が
      // 同じ列に入ることになる（在籍者数・定員・乗降客数に続いて）。
      const isNoise = p.layer === "noise";
      const isPark = p.layer === "park";
      let size = "";
      if (isNoise) {
        const years = p.years ? `${p.years}年度` : "";
        size =
          `<span class="fl-size">${escapeHtml(String(p.laeq_db ?? "—"))} dB (LAeq)</span>` +
          (years ? `<span class="fl-years">${escapeHtml(years)}</span>` : "");
      } else if (isPark) {
        // **面積と等価半径を並べる。** 面積だけだと 1,096m² が広いのか
        // 狭いのか読めないが、「半径 18m 相当の円」と添えれば、
        // 250m の区画に対してどれだけかが一目で分かる。
        const a = Number(p.area_m2 ?? 0).toLocaleString("ja-JP");
        size =
          `<span class="fl-size">${a} m²</span>` +
          `<span class="fl-years">半径 ${escapeHtml(String(p.r_m ?? "—"))}m 相当の円</span>`;
      } else {
        const spec = LIST_CAPACITY[String(p.layer ?? "")];
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
      }
      // **一覧でも調査名を出す。** 60 行を眺めているときに、どの点が
      // 「苦情の出た道路」でどれが系統調査なのかが行ごとに分からないと、
      // 混ぜたことが見えない（docs/issues.md A1）。
      const meta = isNoise
        ? `騒音の測定地点${p.survey ? ` ・ ${p.survey}` : ""}`
        : isPark
          ? "公園（退避先として評価したものではありません）"
          : String(p.host_kind ?? p.kind ?? "");
      return `<li class="fl-item"${hasPoint ? ` data-i="${index}"` : ""} role="button" tabindex="0">
          <div class="fl-name">${escapeHtml(String(p.name ?? ""))}</div>
          <div class="fl-meta">${escapeHtml(meta)}${
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

  const isGreen = state.highlight === "green";
  return `<div class="fact-list">
      <div class="fl-head">${fc.features.length.toLocaleString("ja-JP")} 件（地図に出ている点と同じ）・近い順
        <span class="fl-hint">名前を押すと地図が寄って吹き出しが出ます${
          isGreen
            ? "。地図の円は面積の等しい円で、公園の実際の形ではありません"
            : ""
        }</span></div>
      <ul class="fl-list">${rows}</ul>
      ${sources.length ? `<div class="fl-source">出典: ${boldMd(sources.join(" / "))}</div>` : ""}
    </div>`;
}

/**
 * 内訳の 1 行に「で、何位なの？」を出す。
 *
 * **画面は「地域内順位（3,626区画中）」という札を出しながら、
 * 順位そのものをどこにも書いていなかった**（2026-08-05・指摘を受けて追加）。
 * 母数だけがあって順位が無く、代わりに 0〜1 の正規化値が出ていた——
 * 0.995 が何位かは母数を掛ければ出せるが、読む側にそれをさせていた。
 *
 * **絶対尺度の層では順位を主にしない。** その層の値を決めているのは
 * 法令の物差しであって順位ではない。順位は「23 区の中でどのあたりか」を
 * 言うだけの参考として、物差しの上の位置の後ろに置く。
 */
function layerRankLine(c: ComponentDef, state: AppState, idx: number): string {
  const lr = state.layerRanks[c.key];
  if (!lr) return "";
  const rank = lr.rank[idx];
  const n = state.meta.mesh_count;

  if (c.absolute) {
    const value = (state.rows[idx][`n_${c.key}`] as number) ?? 0;
    return `<div class="factor-rank">
        <span class="fr-main">物差しの上の位置 <b>${fmt(value)}</b></span>
        <span class="fr-sub">${escapeHtml(c.absolute.label)}。
          値はこの物差しで決まり、順位では決まりません
          （参考: ${num(n)} 区画中 第 ${num(rank)} 位）。</span>
      </div>`;
  }
  if (!rank) {
    // 順位が無い＝その層の値が 0。**「最下位」ではない**——順位を付ける
    // 母集団に入っていない（掛け算モデルで確実に 0 を効かせるため）。
    return `<div class="factor-rank">
        <span class="fr-main">順位なし</span>
        <span class="fr-sub">この層の値が 0 の区画です。
          ${num(lr.denom)} 区画の順位付けには入っていません
          （最下位ではなく、母集団の外）。</span>
      </div>`;
  }
  return `<div class="factor-rank">
      <span class="fr-main"><b>${num(lr.denom)} 区画中 第 ${num(rank)} 位</b>
        （上位 ${pctRank(rank, lr.denom)}%）</span>
      <span class="fr-sub">母数はこの層の値が 0 でない区画。
        全 ${num(n)} 区画ではありません。</span>
    </div>`;
}

function renderSelected(
  body: HTMLElement,
  state: AppState,
  onHighlight: (kind: HighlightKind | null) => void,
  onFocusPoint: (index: number) => void,
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

  // --- 8 レイヤーの内訳（実数と正規化値を 1 行に並べる） ---
  //
  // **かつては「この区画の実数」と「需要側／負荷側の内訳」が別の節だった。**
  // 同じ 8 層が画面の 2 箇所に、しかも別の順序で出ていた——実数の側は
  // 半径ごと、内訳の側は寄与の大きい順。**「事業所 60件」と
  // 「事業所 0.995」が離れた場所にあり、どちらがどちらの根拠なのか
  // 読めなかった。** 1 行に畳んで対応を自明にする。
  //
  // **並び順は meta.components そのまま**（= config.py の定義順 =
  // 左のスライダー・算出方法の一覧と同じ）。寄与の大きい順に並べ替えると、
  // 区画を選び直すたびに行が入れ替わり、**区画どうしを見比べられない**。
  // 大きさは棒が示すので、順序に情報を持たせる必要が無い。
  for (const side of ["demand", "load"] as const) {
    const comps = meta.components.filter((c) => c.side === side);

    // **重みを動かしたときに動く量を、この節の中に持つ。**
    //
    // スライダーを動かしても内訳の数値が動かないのは直感に反する、という
    // 指摘を受けた（2026-08-05）。動かないのは正しい——**層の正規化値は
    // その区画の性質**で、こちらが何を重視するかとは関係が無い。
    // だが画面はそれを言っておらず、上の需要・負荷・順位だけが動いていた。
    //
    // 動くものをこの節に出す: 重み・寄与（正規化値 × 重み）・
    // その側の寄与に占める割合。**寄与は重み付き和の中の取り分**であって、
    // 需要スコアそのものではない（和はこのあと順位化される）。
    const contributions = comps.map(
      (c) => ((row[`n_${c.key}`] as number) ?? 0) * (weights[c.key] ?? c.weight),
    );
    const totalAbs = contributions.reduce((a, v) => a + Math.abs(v), 0);

    const section = document.createElement("div");
    section.className = "factors";
    section.innerHTML =
      `<h2 style="margin-top:14px">${side === "demand" ? "需要側" : "負荷側"}の内訳` +
      `（${comps.length} レイヤー）</h2>` +
      '<p class="factor-source" style="margin:-4px 0 8px">' +
      (side === "demand"
        ? "<b>下線のある行を選ぶと、数えたものが下に一覧で開き、同時に地図にも出ます。</b>" +
          "一覧の行数と地図の点の数は、行に書いた件数と必ず一致します。<br>"
        : "") +
      "<b>スライダーを動かしても、左の実数と正規化値は動きません</b>——" +
      "それはこの区画の性質で、こちらが何を重視するかとは無関係だからです。" +
      "動くのは<b>寄与</b>（正規化値 × 重み）と、上の需要・負荷・順位です。</p>";

    comps.forEach((c, ci) => {
      const w = weights[c.key] ?? c.weight;
      const normalized = (row[`n_${c.key}`] as number) ?? 0;
      const fact = layerFact(c.key, row, meta.fact_radius_m);
      const on = fact.hl && state.highlight === fact.hl.kind;
      const contribution = contributions[ci];
      const share = totalAbs > 0 ? Math.abs(contribution) / totalAbs : 0;

      const el = document.createElement("div");
      el.className = "factor" + (fact.hl ? " is-highlightable" : "") + (on ? " is-on" : "");
      if (fact.hl) {
        el.dataset.hl = fact.hl.kind;
        el.setAttribute("role", "button");
        el.setAttribute("tabindex", "0");
        el.setAttribute("aria-expanded", on ? "true" : "false");
      }
      // 重みが 0 の層は、スコアに効いていないことを行の側で言う。
      // 消してしまうと「そもそも測っていない」と読めてしまう。
      const zero = w === 0 ? '<span class="factor-zero">重み 0</span>' : "";
      el.innerHTML = `
        <div>
          <div class="factor-label">${escapeHtml(c.label)} ${scaleTag(c)}${zero}</div>
          <div class="factor-fact">${escapeHtml(fact.value)}<span class="factor-basis">${escapeHtml(fact.basis)}</span></div>
          <div class="factor-bar${c.sign < 0 ? " is-negative" : ""}">
            <i style="width:${Math.round(normalized * 100)}%"></i>
          </div>
          ${layerRankLine(c, state, idx)}
          <div class="factor-weighted">
            <span class="fw-label">この重みでの寄与</span>
            <span class="fw-calc">${fmt(normalized)} × 重み ${fmt(w, 1)} =</span>
            <b class="fw-value">${
              // 0 に符号を付けない。減点レイヤーの寄与が 0 のとき
              // 「−0.000」と出ていて、引かれているように見えた。
              contribution === 0 ? "" : c.sign < 0 ? "−" : ""
            }${fmt(Math.abs(contribution))}</b>${
              c.sign < 0 ? '<span class="factor-zero">減点</span>' : ""
            }
            <span class="fw-share">${side === "demand" ? "需要側" : "負荷側"}の寄与の ${Math.round(
              share * 100,
            )}%</span>
            <span class="factor-bar is-contribution${c.sign < 0 ? " is-negative" : ""}">
              <i style="width:${Math.round(share * 100)}%"></i>
            </span>
          </div>
          <div class="factor-source">出典: ${boldMd(c.source)}</div>
        </div>
        <div class="factor-num">${fmt(normalized)}</div>`;
      section.appendChild(el);
      if (on) section.insertAdjacentHTML("beforeend", facilityListBlock(state, row));
    });
    body.appendChild(section);
  }

  // --- スコアに入らない 2 つ ---
  //
  // **この 2 つを内訳に混ぜてはいけない。** どちらも重みを持たず、
  // 優先度に 1 ミリも寄与しない。上の 8 行と同じ見た目で並べると、
  // 「9 番目・10 番目のレイヤー」に見える。
  const extra = document.createElement("div");
  extra.className = "factors";
  extra.innerHTML =
    '<h2 style="margin-top:14px">スコアに入らない値</h2>' +
    '<p class="factor-source" style="margin:-4px 0 8px">' +
    "下の 2 つは重みを持たず、優先度に寄与しません。</p>";

  const host = hostFact(row, meta.fact_radius_m);
  const near = nearestStationFact(row, meta.fact_radius_m);
  for (const [item, basis] of [
    [
      host,
      `到達可否の境目（半径 ${num(meta.fact_radius_m.host)}m）。` +
        "帯域ではなく「徒歩圏に 1 件も無い＝到達不可」という判定の距離です。" +
        "23 区が公開する一覧のみで、都・国・民間の施設は入っていません。",
    ],
    ...(near ? [[near, "順位表の見出しに使う、この区画の呼び名です。"] as const] : []),
  ] as [FactItem, string][]) {
    const on = item.hl && state.highlight === item.hl.kind;
    const el = document.createElement("div");
    el.className = "factor" + (item.hl ? " is-highlightable" : "") + (on ? " is-on" : "");
    if (item.hl) {
      el.dataset.hl = item.hl.kind;
      el.setAttribute("role", "button");
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-expanded", on ? "true" : "false");
    }
    el.innerHTML = `
      <div>
        <div class="factor-label">${escapeHtml(item.label)}</div>
        <div class="factor-fact">${escapeHtml(item.value)}<span class="factor-basis">${escapeHtml(basis)}</span></div>
      </div>
      <div class="factor-num"></div>`;
    extra.appendChild(el);
    if (on) extra.insertAdjacentHTML("beforeend", facilityListBlock(state, row));
  }
  body.appendChild(extra);

  // --- 行と一覧の配線 ---
  //
  // 行を押すとハイライトが切り替わり、一覧が開く／閉じる。
  // **一覧の項目は行の外側にある別の要素**（同じ要素に入れていたら、
  // 一覧を触るたびに一覧が閉じる）。
  for (const el of body.querySelectorAll<HTMLElement>(".factor[data-hl]")) {
    const kind = el.dataset.hl as HighlightKind;
    const fire = () => onHighlight(state.highlight === kind ? null : kind);
    el.addEventListener("click", fire);
    el.addEventListener("keydown", (e) => {
      if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") {
        e.preventDefault();
        fire();
      }
    });
  }
  for (const li of body.querySelectorAll<HTMLElement>(".fl-item[data-i]")) {
    const index = Number(li.dataset.i);
    const fire = () => onFocusPoint(index);
    li.addEventListener("click", fire);
    li.addEventListener("keydown", (e) => {
      if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") {
        e.preventDefault();
        fire();
      }
    });
  }
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
      "騒音は幹線道路の道路端で測った屋外の値（常時監視測定・令和元〜5年度の" +
        "5 年分 700 点）で、繁華街の雑踏も、建物の中の音環境も含まない。" +
        "常時監視は幹線道路を年度ごとにローテーションして測るため、" +
        "5 年分を重ねて測定点の密度を上げている（1 年度は約 150 点）。",
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
    // **この層は 2 つの意味を名乗っていた**（2026-08-06 に整理）。
    // rationale は「既に安らげる空間が担保されている」＝供給の話だったのに、
    // 実装は区画に重なる面積の割合＝負荷の低減である。根拠文が
    // 「屋外に代替の退避先が存在しない」と書けていたのはそのためで、
    // **被覆の値を供給の言葉で説明していた。** 負荷の低減に統一し、
    // 元データの弱さもここに出す。
    [
      "緑・公園は「区画に重なる面積」で、行ける公園ではない",
      "この層が見ているのは 250m 区画に重なる公園の面積割合だけで、" +
        "**隣の区画にある公園は数えていない**（他の 4 層のような徒歩圏では" +
        "判定していない）。負荷を下げる要素として扱っており、" +
        "**退避先として評価したものではない**——屋外がその役を果たせるかは" +
        "当事者に確かめていない。さらに元データ（国土数値情報 P13・2011年）は" +
        "**点で、公園の形が入っていない**。面積の等しい円に置き換えており、" +
        "**等価半径の中央値は 18m**（3,835 件のうち 3,738 件は区画より小さい円）。" +
        "内訳の「緑・公園被覆」を押すと、その円を地図で確かめられる。",
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

  // **畳んだ状態でも、いちばん重い数字は見えていること。**
  // 折りたたみは「読まなくてよい」という意味になりがちなので、
  // 要約行に残す——ここを隠すと、順位の不安定さが 1 クリック
  // 向こう側に消える。
  const lede = document.getElementById("robustness-lede");
  if (lede) {
    lede.innerHTML =
      `重み ±${Math.round(rp.perturbation * 100)}% で上位10件の ` +
      `<b>${Math.round(top10 * 100)}%</b> が残る / ` +
      `${pa.preset_ids.length} つのプリセット共通は <b>${pa.common_count} 件</b>`;
  }

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
         ${boldMd(c.rationale)}<br>
         ${scaleBadge(c)}<br>
         <span class="factor-source">出典: ${boldMd(c.source)}</span></li>`,
    )
    .join("");

  const absolute = meta.components.filter((c) => c.absolute);

  // **出典名とライセンス条件だけでは、CC BY の要求を満たさない。**
  // 求められているのは「著作権表示（作者名）／作品タイトル／ライセンス URL」で、
  // ここには作者名が無く、ライセンスは押せない文字列だった（2026-08-13 に是正）。
  // **作者名は経由したカタログの名前ではない**——公共施設一覧は
  // 「東京都オープンデータカタログ CC BY 4.0」と名乗っていたが、
  // 著作者は 23 の区それぞれである。
  const sources = meta.sources
    .map(
      (s) =>
        `<tr>
           <th style="text-transform:none;letter-spacing:0">
             ${s.url ? `<a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.label)}</a>` : escapeHtml(s.label)}
             ${s.rights_holder ? `<span class="factor-source">${escapeHtml(s.rights_holder)}</span>` : ""}
             <span class="factor-source">${
               s.license_url
                 ? `<a href="${escapeAttr(s.license_url)}" target="_blank" rel="noopener">${escapeHtml(s.license)}</a>`
                 : escapeHtml(s.license)
             }</span>
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
    <!--
      **「なぜ 2 種類が混ざっているのか」を画面が答えていなかった。**
      札は出していたし、それぞれが何であるかも書いてあったが、
      混ぜた理由と、混ぜたことの代償が無かった
      （2026-08-05・「どういう意図で、それは妥当なのか」と指摘されて追加）。
    -->
    <h2 style="margin-top:14px">なぜ 2 種類の物差しが混ざっているのか</h2>
    <p class="card-narrative">
      <b>順位化には副作用があります。</b>パーセンタイル順位は、その層の値が
      対象地域内でどれだけ狭い範囲に収まっていても、必ず 0〜1 いっぱいに
      引き伸ばします。<b>識別力の無い層ほど差が誇張され、測定の偏りがあれば
      それごと増幅されます。</b>
    </p>
    <p class="card-narrative">
      実際にこの 2 層で起きました。<b>用途地域</b>は用途制限の強さから
      0.05〜1.00 の段差を意図して置いた値なのに、順位化を通すと第一種低層住居
      専用地域が 0.285 で乗っていました（設計値の約 6 倍。「静穏が法的に
      保証された土地」が商業地域の 3 割の負荷を持つ）。<b>騒音</b>は
      対象 2 区の頃、内挿値のばらつきが標準偏差 2.24dB しか無いのに 0〜1 へ広げられ、
      <b>測定点の選ばれ方の偏りが、そのまま区の違いとして働いていました</b>。
    </p>
    <p class="card-narrative">
      <b>では全部を絶対尺度にすればよいのでは、とはなりません。</b>
      絶対尺度に移せるのは、<b>0 と 1 を外部の法令・告示で決められる層だけ</b>です。
      騒音は環境基準 55dB と要請限度 75dB、用途地域は建築基準法別表第二から
      取っています。一方、事業所の定員や駅の乗降客数に「この値が 1」と言える
      外部の基準はありません。無いのに決めれば、
      <b>順位化の恣意性を別の恣意性に置き換えるだけ</b>になります。
      だから<b>根拠を書ける層だけを移し、書けない層は順位のまま残しています</b>。
    </p>
    <p class="card-narrative">
      <b>混ぜたことの代償は 2 つあります。</b>
      1 つは、<b>層をまたいで「0.5」を同じ意味に読めないこと</b>——
      順位の層の 0.5 は「真ん中あたり」、絶対尺度の層の 0.5 は
      「物差しの中点（騒音なら 65dB）」で、別のものです。
      重みを層どうしで比べるときは、この違いが入っています。
      もう 1 つは下に書いた通りで、<b>絶対尺度の「変わらない」は
      最終スコアまでは残りません</b>。
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
    <p class="card-narrative">
      出典の数（${meta.sources.length} 件）は、上の
      ${meta.components.length} 層とも、データの層
      ${meta.layer_roles.length} 個とも一致しません。
      <b>1 つの層を複数の出典が書いていることがあるためです</b>——
      特別支援学校は位置（国土数値情報 P29）と規模（都教委の在籍者数）を
      別々の出典から取り、既存の公共施設は 23 区の一覧と児童館（P14）を
      合わせています。
    </p>
    <!--
      **「他の自治体でも回せるのか」は必ず聞かれる。** 方法を主役に置く以上、
      そこが主張の一部になる。**答えられないより、正直に答えるほうが強い**
      ——回した実績が無いことも含めて書く（docs/reproducibility.md）。
    -->
    <p class="card-narrative">
      <b>更新の手順。</b>集計・スコア・配信・検査はコマンド 1 本で通ります
      （<code>python -m etl.build --live</code>）。<b>人手が要るのは
      生ファイルを置く段だけ</b>で、そこを自動化していないのは、
      出典の側が年度ごとに URL とファイル構成を変えるためです——
      決め打ちで取りに行くと、年度が変わった瞬間に<b>古いデータで静かに通る</b>
      ビルドになります。止まるほうを選んでいます。
      <br><br>
      いちばん重いのは供給側で、<b>区の公共施設一覧は 23 区で約 30 本を
      毎回すべて渡します</b>（統合一覧を持たない区・KML の区・xlsx の区・
      名称の列名が「列1」の区・座標が無い区がそれぞれあります）。
      対象地域を変えるときに書き換えるのは <code>etl/config.py</code> の
      3 か所ですが、<b>移した先では重みを決め直す必要があります</b>——
      この 8 つの重みに外部の根拠は無いので、そのまま使うと
      出てくるのは東京の価値判断です。
      <br><br>
      <b>他の地域で実際に回したことはまだありません。</b>
    </p>
    <!--
      **改変した旨の明記は、表の下ではなく上に置く。** CC BY・国土数値情報の
      利用約款（PDL1.0）・e-Stat 利用規約がそろって明文で求めている条件で、
      **表を読む前に効いていないと意味がない**——読み手が下の数字を
      「出典がそう言っている」と読んだあとで但し書きが来ても遅い。
      文言は etl/config.py の MODIFICATION_NOTICE が唯一の出所。
    -->
    <p class="card-narrative">${boldMd(meta.modification_notice)}</p>
    <table class="data-table">${sources}</table>
    <p class="card-narrative">
      レジストリには他に ${meta.unused_source_count} 件の出典がありますが、
      <b>検討しただけで使っていない</b>ため、ここには出していません。
    </p>
    <p class="card-narrative">
      データ生成: ${escapeHtml(meta.generated_at)} / モード: ${meta.data_mode}
    </p>`;
}

/**
 * `**強調**` だけを太字に戻す。**それ以外は素通しにしない。**
 *
 * 出典名や rationale は `etl/config.py` が唯一の出所で、そちらでは
 * 日本語の文章として `**` を使って強調を書いている。画面はそれを
 * エスケープしたまま出していたので、**アスタリスクがそのまま見えていた**
 *（プリセットの note では既に戻していたのに、rationale では戻していなかった）。
 * 先にエスケープしてから太字だけ戻すので、config 側に HTML は書けない。
 */
function boldMd(s: string): string {
  return escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

/**
 * `title` 属性など、**太字にできない場所**のための素通し版。
 *
 * 属性値に `<b>` は書けないので、記号だけを落として本文を残す。
 * `boldMd` と対で使い、**`escapeHtml` を素で当てる場所を出典に残さない**
 * ——残っていたせいで、負荷レイヤーの出典が画面に
 * `——**道路端の測定点 6 年分**` とアスタリスクごと出ていた。
 */
function plainMd(s: string): string {
  return String(s ?? "").replace(/\*\*(.+?)\*\*/g, "$1");
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
