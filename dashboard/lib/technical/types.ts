/** Stored breakdown_json.schema_version === 1 */
export interface TechnicalBreakdownJson {
  schema_version?: number;
  regime?: string;
  bull_strength?: number;
  bear_strength?: number;
  confidence?: number;
  indicator_snapshot?: Record<string, number>;
  weighted_bull?: number;
  weighted_bear?: number;
  max_weight_total?: number;
  bull_reasons?: string[];
  bear_reasons?: string[];
}

export interface DashboardRunMeta {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  scorer_version: string;
  tickers_requested: number;
  tickers_succeeded: number;
}

export interface StockSnapshotRow {
  id: string;
  dashboard_run_id: string;
  symbol: string;
  computed_at: string;
  daily_bar_end: string | null;
  h4_bar_end: string | null;
  regime: string;
  bull_strength: number;
  bear_strength: number;
  confidence: number;
  breakdown_json: TechnicalBreakdownJson;
}
