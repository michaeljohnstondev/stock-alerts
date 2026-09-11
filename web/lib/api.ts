/**
 * Typed client for the FastAPI layer.
 *
 * These interfaces mirror api/models.py by hand. They could be generated
 * from the OpenAPI spec the API already publishes at /openapi.json --
 * worth doing once the shapes stop moving.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export interface Stats {
  tickers: number;
  rating_levels: number;
  runs: number;
  alert_events: number;
  price_observations: number;
  latest_run_at: string | null;
  history_days: number;
}

export interface Run {
  id: number;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  tickers_scanned: number | null;
  api_calls: number | null;
  market_trends: Record<string, string>;
  report_path: string | null;
}

export interface Alert {
  symbol: string;
  sector: string | null;
  direction: "buy" | "sell";
  rung: number;
  target_price: number;
  price: number;
  pct_away: number;
  triggered: boolean;
  escalated: boolean;
  prev_rung: number | null;
  observed_at: string;
  grade: string | null;
  value_score: number | null;
}

export interface Rung {
  direction: "buy" | "sell";
  rung: number;
  price: number;
  source: string;
  pct_away: number | null;
  triggered: boolean | null;
}

export interface PricePoint {
  observed_at: string;
  price: number;
}

export interface TickerDetail {
  symbol: string;
  sector: string | null;
  industry: string | null;
  price: number | null;
  observed_at: string | null;
  grade: string | null;
  value_score: number | null;
  valuation: number | null;
  profitability: number | null;
  health: number | null;
  growth: number | null;
  forward_pe: number | null;
  market_cap: number | null;
  ladder: Rung[];
  price_history: PricePoint[];
}

export interface GradeBucket {
  grade: string;
  names: number;
  avg_score: number | null;
}

export interface SectorRow {
  sector: string;
  rank: number;
  composite: number;
  status: string;
  captured_at: string;
  rank_change: number | null;
}

/**
 * All reads go through here.
 *
 * `no-store` because this is live market data -- a cached dashboard
 * showing yesterday's triggers would be worse than no dashboard.
 */
async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`${path} responded ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  stats: () => get<Stats>("/stats"),
  runs: (limit = 10) => get<Run[]>(`/runs?limit=${limit}`),
  grades: () => get<GradeBucket[]>("/grades"),
  sectors: () => get<SectorRow[]>("/sectors"),
  symbols: () => get<string[]>("/symbols"),
  ticker: (symbol: string) =>
    get<TickerDetail>(`/tickers/${encodeURIComponent(symbol)}`),
  alerts: (params: {
    direction?: "buy" | "sell";
    minRung?: number;
    minScore?: number;
    sector?: string;
    escalatedOnly?: boolean;
    limit?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.direction) q.set("direction", params.direction);
    if (params.minRung) q.set("min_rung", String(params.minRung));
    if (params.minScore) q.set("min_score", String(params.minScore));
    if (params.sector) q.set("sector", params.sector);
    if (params.escalatedOnly) q.set("escalated_only", "true");
    q.set("limit", String(params.limit ?? 100));
    return get<Alert[]>(`/alerts?${q.toString()}`);
  },
};

/** A/A+ green through F red, for value grades. */
export function gradeColor(grade: string | null): string {
  if (!grade) return "text-neutral-500";
  const letter = grade[0];
  if (letter === "A") return "text-emerald-400";
  if (letter === "B") return "text-lime-400";
  if (letter === "C") return "text-amber-400";
  if (letter === "D") return "text-orange-400";
  return "text-red-400";
}

export function fmtMoney(v: number | null | undefined): string {
  if (v == null) return "—";
  return v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 })
                   : v.toFixed(2);
}

export function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}
