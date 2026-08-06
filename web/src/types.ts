/** ETL (etl/build.py) が出力する JSON の型定義。 */

export type Side = "demand" | "load";

export interface ComponentDef {
  key: string;
  label: string;
  /**
   * 元になっているデータの層（meta.layer_provenance のキー）。
   * **「実データ 10/10 レイヤー」と「評価に使う 8 つのレイヤー」の
   * 対応を画面で言うために要る**——どちらも正しいのに、対応が
   * どこにも書かれていなかった（etl/config.py の SUPPORT_LAYERS）。
   */
  layer: string;
  side: Side;
  /** 既定重み。スライダーの初期値。 */
  weight: number;
  /** +1 = 加点、-1 = 減点レイヤー（緑被覆など）。 */
  sign: number;
  /** 値 0 を「不在」として厳密に 0 に落とすか。etl/config.py と対応。 */
  zeroIsAbsence: boolean;
  source: string;
  rationale: string;
  /**
   * 順位の母数。**「地域内順位」と書くだけでは足りない**——値 0 は
   * 「存在しない」として厳密に 0 に固定し、正の値を持つ区画の中だけで
   * 順位を付けているので、母数は層ごとに違う（特別支援学校は 3,626、
   * 障害福祉サービス事業所は 9,168）。絶対尺度の層は null。
   */
  rank_denominator: number | null;
  /**
   * 非 null なら、この層は対象地域内の順位ではなく外部の基準で 0〜1 に
   * 正規化されている（騒音は環境基準、用途地域は用途制限の強さ）。
   * 「順位は対象地域内の相対値」という但し書きがこの層には掛からない。
   */
  absolute: AbsoluteScale | null;
}

/** 外部の基準に固定した正規化尺度。etl/config.py の AbsoluteScale と対応。 */
export interface AbsoluteScale {
  /** UI 表示用の短い説明。例「環境基準 55dB → 0 / 要請限度 75dB → 1」 */
  label: string;
  lo: number;
  hi: number;
  /** lo / hi をどの法令・告示から取ったか。 */
  basis: string;
}

export interface Meta {
  generated_at: string;
  data_mode: "live" | "fixture";
  synthetic: boolean;
  synthetic_notice: string | null;
  /** レイヤー名 → "real" | "synthetic"。1レイヤーずつ実データへ差し替わる。 */
  layer_provenance: Record<string, "real" | "synthetic">;
  real_layer_count: number;
  layer_total: number;
  target_wards: string[];
  bbox: [number, number, number, number];
  mesh_level: number;
  mesh_label: string;
  mesh_count: number;
  priority_alpha: number;
  priority_beta: number;
  host_max_distance_m: number;
  /**
   * 「この区画の実数」の各行が、どの半径で数えた値か。etl/build.py が出す。
   * 画面はここから節見出しを作る（半径を TypeScript に直接書くと、
   * 帯域を動かしたときにラベルだけが古い値を主張し続ける）。
   */
  /**
   * 実数を数えた半径。**welfare / school / clinic / station は
   * その層がスコアに使っている帯域そのもの**で、層ごとに違うのはそのため。
   * host だけ種類が違う（スコアに入らない。到達可否の境目）。
   */
  fact_radius_m: {
    welfare: number;
    school: number;
    clinic: number;
    /** 駅の帯域（600m）。他の 3 層と同じ規則で、件数を数える半径。 */
    station: number;
    host: number;
    /**
     * 最寄り駅を探す上限。**件数の半径ではない**——順位表の見出しに使う
     * 「この区画の呼び名」を決めるためだけの距離で、スコアには入らない。
     */
    station_max: number;
    /**
     * 騒音の内挿（IDW）の打ち切り距離。**帯域でも徒歩圏でもない**——
     * この距離の内に測定点が 1 つも無ければ、その区画の騒音は測定ではなく
     * 23 区の中央値である（`f_noise_n` が 0 の 496 区画）。
     */
    noise: number;
  };
  /**
   * データの層 10 個の役割。**8 と 10 の食い違いを画面で解くために配信する。**
   * role="score" が 8 層、role="support" が 2 層（既存の公共施設・区界）。
   * 分類漏れがあれば build.py が止まるので、ここが古くなることはない。
   */
  layer_roles: {
    layer: string;
    label: string;
    role: "score" | "support";
    /** スコアに入る層なら対応する構成要素のキー。入らない層は null。 */
    component: string | null;
    side: Side | null;
    /** スコアに入らない層が何をしているか。role="score" では空。 */
    note: string;
    provenance: "real" | "synthetic";
  }[];
  /**
   * 「既存施設では到達不可」の要約。**重みにもスコアにも依存しない**ので
   * 画面の見出しに使う。mid_or_above は区内の優先度の中央値で切った件数で、
   * しきい値の取り方が込み入っているため Python 側でだけ計算する
   * （TypeScript に書き直すと静かに食い違う）。
   */
  unreachable: {
    count: number;
    ratio: number;
    mid_or_above: number;
    note: string;
  };
  /**
   * 既に置かれているカームダウンスペースとの照合（この作品で唯一の外部照合）。
   * 入力は手で集めた一覧（`data/reference/calm_spaces.json`）で、
   * **網羅性の保証は無く、スコアには一切入らない。**
   * ビルドが無い版（--live を通していない出力）では欠ける。
   */
  calm_spaces?: {
    surveyed_at: string;
    site_count: number;
    room_count: number;
    /** access 別の室数。open / members / ticketed / airside。 */
    rooms_by_access: Record<string, number>;
    /** 街を歩いている人がその場で使える室数。 */
    open_rooms: number;
    rank_min: number | null;
    rank_median: number | null;
    rank_max: number | null;
    in_top_50: number;
    in_bottom_half: number;
    mesh_count: number;
    note: string;
  };
  /**
   * 順位表に出す件数の選択肢と既定値。etl/config.py が唯一の出所。
   * **選べるようにしてあるのは、打ち切りに根拠が無いことを隠さないため。**
   */
  ranking_options: number[];
  ranking_default_n: number;
  /** 重みプリセット。etl/config.py の PRESETS が単一の情報源。 */
  presets: { id: string; label: string; note: string; weights: Weights }[];
  components: ComponentDef[];
  /** **実際に使った出典だけ**が入る。検討しただけのものは含まれない。 */
  sources: {
    key: string;
    label: string;
    url: string;
    license: string;
    note: string;
    /** 生成に使ったレイヤー名。 */
    layer: string;
    /** 年次。レイヤーごとに 15 年ぶん開いていること自体が課題（issues.md C1）。 */
    vintage: string;
    count: number | null;
  }[];
  /** レジストリにあるが使っていない出典の件数。「N 出典を使った」と誤読させないため。 */
  unused_source_count: number;
  layer_counts: Record<string, number>;
}

/** mesh.geojson の feature properties。`c` はメッシュコード。 */
export interface MeshProps {
  c: string;
  demand: number;
  load: number;
  priority: number;
  /** その区画の区名。meta.target_wards への添字で届く（配信量を抑えるため）。 */
  w?: number;
  /** 徒歩圏（meta.host_max_distance_m）にある区の公共施設の件数。 */
  f_host_n?: number;
  /**
   * この区画の騒音値を作るのに使った測定点の件数（打ち切り 1,500m 以内）。
   * **0 は「静か」ではなく「測っていない」**——その区画の f_noise_db は
   * 23 区の中央値で補完した値である（docs/issues.md A6）。
   */
  f_noise_n?: number;
  /**
   * 徒歩圏の障害福祉事業所のうち、対象 23 区の外にあるものの件数。
   * 入力は区界の外側 2km まで拾う設計なので（エッジ効果の回避）、
   * 外周のメッシュは隣接自治体の施設で需要が決まっていることがある。
   */
  f_welfare_outside_n?: number;
  /** 徒歩圏の公共施設のうち代表 1 件。設置先の選定ではなく例示。 */
  host?: string;
  host_kind?: string;
  host_ward?: string;
  host_d?: number;
  ward_coefficient?: number;
  /** n_<component key> が構成要素の数だけ入る。 */
  [key: string]: string | number | undefined;
}

export interface Factor {
  key: string;
  label: string;
  normalized: number;
  contribution: number;
  raw: number;
  source: string;
  rationale: string;
}

export interface Card {
  rank: number;
  mesh_code: string;
  lon: number;
  lat: number;
  /** その区画の区名（施設の区名ではない）。 */
  ward: string;
  station: string;
  priority: number;
  demand: number;
  load: number;
  host_count: number;
  host_name: string;
  host_kind: string;
  host_ward: string;
  host_distance_m: number | null;
  demand_factors: Factor[];
  load_factors: Factor[];
  narrative: string;
}

/**
 * proposals.json の 1 行。**単位は区画**（地区でも施設でもない）。
 * 束ねるのをやめた理由は etl/hosts.py の build_ranking を参照。
 */
export interface Proposal {
  rank: number;
  mesh_code: string;
  lon: number;
  lat: number;
  ward: string;
  station: string;
  priority: number;
  demand: number;
  load: number;
  host_count: number;
  host_name: string;
  host_kind: string;
  host_ward: string;
  host_distance_m: number | null;
  /** 徒歩圏に区の公共施設が 1 件も無い。 */
  unreachable: boolean;
  /** 上位 adjacent_of 区画のうち、この区画に接しているものの数。 */
  adjacent_n: number;
  /** adjacent_n の母数。**これが無いと隣接数が場所の性質に見える。** */
  adjacent_of: number;
  narrative: string;
}

export type Weights = Record<string, number>;

/** sensitivity.json。感度分析の結果（任意・無ければ表示しない）。 */
export interface Sensitivity {
  /**
   * meta.json と同じビルドで作られたことの照合キー。
   * `--sensitivity` を付けたときだけ書かれるため、現物が別ビルドのものに
   * なり得る。**古い感度分析を無言で出さない**ために突き合わせる。
   */
  generated_at?: string;
  /** 重み以外の固定値を揺さぶった結果（issues.md A3・D1）。 */
  fixed_values?: {
    perturbation: number;
    trials: number;
    assumed_capacity_rows: number;
    groups: {
      id: string;
      label: string;
      constants: number;
      overlap_mean: Record<string, number>;
      overlap_min: Record<string, number>;
      rank_shift_median: number;
    }[];
    scenarios: {
      id: string;
      label: string;
      overlap: Record<string, number>;
      rank_shift_median: number;
    }[];
  };
  host_distance?: {
    radius_m: number;
    unreachable: number;
    unreachable_ratio: number;
    mid_or_above: number;
  }[];
  random_perturbation: {
    perturbation: number;
    trials: number;
    overlap_mean: Record<string, number>;
    overlap_min: Record<string, number>;
    rank_shift_median: number;
    rank_shift_p90: number;
  };
  leave_one_out: {
    key: string;
    label: string;
    side: string;
    weight: number;
    overlap_top10: number;
  }[];
  preset_agreement: {
    top_k: number;
    preset_ids: string[];
    common_count: number;
    common_ratio: number;
    common_meshes: string[];
  };
}
