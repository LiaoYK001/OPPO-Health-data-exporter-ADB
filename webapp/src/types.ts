export type Fidelity =
  | "exact_source"
  | "exact_ui"
  | "visual_only"
  | "no_data"
  | "failed";

export interface SummaryField {
  key: string;
  label: string;
  unit: string;
}

export interface MetricDefinition {
  key: string;
  name: string;
  short_name: string;
  category: string;
  color: string;
  accent: string;
  primary_field: string;
  secondary_field?: string;
  primary_unit: string;
  series_kinds: string[];
  interval_kinds?: string[];
  storage_metric?: string;
  long_term_stats?: boolean;
  hidden?: boolean;
  summary_fields: SummaryField[];
  availability?: {
    day_count: number;
    earliest_date: string | null;
    latest_date: string | null;
    partial_days: number;
    covered_days: number;
  };
}

export interface DayRecord {
  source_id: number;
  source_name: string;
  vendor: string;
  canonical_metric: string;
  local_date: string;
  revision: number;
  summary: Record<string, unknown>;
  fidelity: Fidelity;
  partial: boolean;
  created_at: string;
}

export interface RangeStatistics {
  field: string;
  unit: string;
  recorded_days: number;
  total: number | null;
  average: number | null;
  minimum: number | null;
  median: number | null;
  maximum: number | null;
  first_date: string | null;
  last_date: string | null;
  secondary?: RangeStatistics;
}

export interface SamplingInfo {
  input_days: number;
  valid_points: number;
  returned_points: number;
  downsampled: boolean;
  method: "none" | "lttb";
  max_points: number;
}

export interface Meta {
  api_version: string;
  app_version: string;
  timezone: string;
  earliest_data_date: string | null;
  latest_data_date: string | null;
  latest_ui_date: string | null;
  latest_normalized_write: string | null;
  latest_ui_write: string | null;
  database_bytes: number;
  source_count: number;
  read_only: boolean;
}

export interface OverviewMetric {
  definition: MetricDefinition;
  days: DayRecord[];
  latest: DayRecord | null;
  day_count: number;
  statistics: RangeStatistics;
  sampling: SamplingInfo;
}

export interface Overview {
  from: string;
  to: string;
  source: string;
  metrics: Record<string, OverviewMetric>;
}

export interface SeriesPoint {
  timestamp_utc_ms: number;
  value: number | string | null;
  unit?: string;
  original_value?: string;
  original_unit?: string;
}

export interface MetricInterval {
  interval_kind?: string;
  kind?: string;
  start_utc_ms: number;
  end_utc_ms: number;
  state?: string | null;
  value_num?: number;
  unit?: string;
  metadata?: Record<string, unknown>;
}

export interface Evidence {
  page_capture_id: number;
  page_key: string;
  fidelity: Fidelity;
  created_at: string;
  role: string;
  viewport_index: number | null;
  sha256: string;
  media_type: string;
  byte_size: number;
  width: number | null;
  height: number | null;
}

export interface DayDetail {
  metric: MetricDefinition;
  date: string;
  source: string;
  data:
    | (DayRecord & {
        series: Record<string, SeriesPoint[]>;
        intervals: MetricInterval[];
        evidence: Evidence[];
      })
    | null;
  candidates: Array<{
    source_id: number;
    source_name: string;
    vendor: string;
    fidelity: Fidelity;
    revision: number;
    priority: number;
    selected: boolean;
    selection_reason: string | null;
  }>;
  coverage: Array<Record<string, unknown>>;
  completeness: {
    summary: boolean;
    series: boolean;
    intervals: boolean;
    evidence: boolean;
    issues: string[];
  };
}

export interface SourceInfo {
  id: number;
  vendor: string;
  app_package: string;
  app_version: string;
  timezone: string;
  display_name: string;
  priority: number;
  enabled: number;
  metrics: Array<{
    canonical_metric: string;
    display_name?: string;
    day_count: number;
    earliest_date: string;
    latest_date: string;
    partial_days: number;
  }>;
  coverage: Array<{ status: Fidelity; count: number }>;
  devices: Array<{
    id: number;
    device_hash: string;
    metadata: Record<string, unknown>;
    created_at: string;
  }>;
}
