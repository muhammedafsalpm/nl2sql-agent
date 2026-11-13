/**
 * Mirrors backend/app/api/schemas.py.
 *
 * These types are maintained by hand — if you change a Pydantic model on the backend,
 * change it here too. `GET /api/info` and `/docs` are the source of truth.
 */

export type Route = "sql" | "clarify" | "chitchat";
export type ChartType = "bar" | "line" | "donut" | "kpi";

export interface Turn {
  user: string;
  assistant: string;
}

export interface ChartSeries {
  name: string;
  data: number[];
}

export interface ChartSpec {
  type: ChartType;
  title: string;
  labels: string[];
  series: ChartSeries[];
  x_axis: string;
  y_axis: string;
  /** Caveats the user must see: "top 8 of 34 shown", "3 non-numeric value(s) plotted as 0". */
  note: string;
}

export interface QueryStep {
  sub_question: string;
  sql: string;
  row_count: number;
  truncated: boolean;
}

export interface DataPayload {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
}

/** "normal" = fast path (~2-3s). "deep" = every agent and check runs (~52s). */
export type Mode = "normal" | "deep";

export interface ChatRequest {
  question: string;
  history: Turn[];
  include_sql: boolean;
  mode: Mode;
}

export interface ChatResponse {
  answer: string;
  route: Route;
  needs_analysis: boolean;
  /** False when the data behind this answer did not pass independent verification. */
  verified: boolean;
  /** Which pipeline produced this answer. */
  mode: Mode;
  latency_seconds: number;
  chart?: ChartSpec | null;
  steps?: QueryStep[] | null;
  data?: DataPayload | null;
  /**
   * Opaque signed handle for POST /api/export. Null when the turn retrieved no data (chitchat,
   * clarify), which is exactly when there is nothing to export — so the Export control can be
   * gated on this alone. Sent regardless of include_sql: it reveals no internals.
   */
  export_token?: string | null;
}

/** File formats POST /api/export accepts. xlsx carries every row; docx caps its table. */
export type ExportFormat = "xlsx" | "docx";

export interface InfoResponse {
  /** Display name of the assistant (APP_NAME in backend/.env). */
  app_name?: string;
  /** One-line description of the connected data (APP_DESCRIPTION), may be empty. */
  app_description?: string;
  provider: string;
  model: string;
  fast_model: string;
  fast_model_enabled: boolean;
  database: string;
  allowed_schemas: string[];
  /** Main entity names in the connected database — used to build starter prompts. */
  entities?: string[];
  max_rows: number;
  max_queries: number;
}

/** One rendered bubble in the transcript. */
export interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  chart?: ChartSpec | null;
  sql?: string[];
  latency?: number;
  route?: Route;
  /** False when the backend could not verify the data behind this answer. */
  verified?: boolean;
  /** Which pipeline produced this answer — shown as a badge so the two are never confused. */
  mode?: Mode;
  /** Present only when this turn returned data; drives whether the Export control is shown. */
  exportToken?: string | null;
  /** Set while the turn is still running, cleared when `done` arrives. */
  pending?: string;
  /** Last real progress stage reported by the backend, kept across heartbeats. */
  stage?: string;
  error?: boolean;
}
