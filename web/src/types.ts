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
  ward: string;
  priority: number;
  demand: number;
  load: number;
  host_name: string;
  host_kind: string;
  host_ward: string;
  host_distance_m: number | null;
  demand_factors: Factor[];
  load_factors: Factor[];
  narrative: string;
}

export interface Proposal {
  host_name: string;
  host_kind: string;
  best_rank: number;
  priority: number;
  lon: number;
  lat: number;
  ward: string;
  covered_meshes: number;
  mesh_codes: string[];
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
