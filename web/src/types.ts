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
  target_wards: string[];
  bbox: [number, number, number, number];
  mesh_level: number;
  mesh_label: string;
  mesh_count: number;
  priority_alpha: number;
  priority_beta: number;
  host_max_distance_m: number;
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
