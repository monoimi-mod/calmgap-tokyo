/** ETL (etl/build.py) が出力する JSON の型定義。 */

export type Side = "demand" | "load";

export interface ComponentDef {
  key: string;
  label: string;
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
  /** 重みプリセット。etl/config.py の PRESETS が単一の情報源。 */
  presets: { id: string; label: string; note: string; weights: Weights }[];
  components: ComponentDef[];
  sources: { key: string; label: string; url: string; license: string; note: string }[];
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
 * 提言の単位は「隣接する上位区画のまとまり（地区）」。
 * 施設単位ではない — 理由は etl/hosts.py の build_proposals を参照。
 */
export interface Proposal {
  /** 例「豊島区 池袋・北池袋周辺」。区名 + 最寄り駅名で組む。 */
  area_label: string;
  ward_label: string;
  ward_counts: Record<string, number>;
  best_rank: number;
  priority: number;
  lon: number;
  lat: number;
  mesh_count: number;
  mesh_codes: string[];
  /** そのうち徒歩圏に区の公共施設が 1 件も無い区画の数。 */
  unreachable_meshes: number;
  /** 各区画の最寄り施設（重複を除く）。設置候補ではなく既存ストックの例示。 */
  facilities: { name: string; kind: string; ward: string }[];
  narrative: string;
}

export type Weights = Record<string, number>;

/** sensitivity.json。感度分析の結果（任意・無ければ表示しない）。 */
export interface Sensitivity {
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
