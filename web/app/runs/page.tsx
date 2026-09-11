import { api } from "@/lib/api";
import { ApiDown, Panel } from "@/components/ui";

export default async function RunsPage() {
  let runs;
  try {
    runs = await api.runs(50);
  } catch (e) {
    return <ApiDown error={e instanceof Error ? e.message : String(e)} />;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Scan runs</h1>
        <p className="mt-1 text-sm text-muted">
          One row per <code className="text-xs">python -m stock_checker</code>.
        </p>
      </div>

      <Panel>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-edge text-left text-xs text-muted">
                <th className="px-4 py-2 font-medium">Run</th>
                <th className="px-4 py-2 font-medium">Started</th>
                <th className="px-4 py-2 text-right font-medium">Duration</th>
                <th className="px-4 py-2 text-right font-medium">Tickers</th>
                <th className="px-4 py-2 text-right font-medium">API calls</th>
                <th className="px-4 py-2 font-medium">Benchmarks</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-edge/60 last:border-0">
                  <td className="tnum px-4 py-2 text-muted">#{r.id}</td>
                  <td className="px-4 py-2">
                    {new Date(r.started_at).toLocaleString()}
                  </td>
                  <td className="tnum px-4 py-2 text-right">
                    {r.duration_seconds != null
                      ? `${Math.floor(r.duration_seconds / 60)}m ${Math.round(r.duration_seconds % 60)}s`
                      : "—"}
                  </td>
                  <td className="tnum px-4 py-2 text-right">{r.tickers_scanned ?? "—"}</td>
                  <td className="tnum px-4 py-2 text-right">{r.api_calls ?? "—"}</td>
                  <td className="px-4 py-2">
                    {Object.keys(r.market_trends ?? {}).length === 0 ? (
                      <span className="text-muted">—</span>
                    ) : (
                      <span className="flex flex-wrap gap-1.5">
                        {Object.entries(r.market_trends).map(([sym, verdict]) => (
                          <span
                            key={sym}
                            className={`rounded px-1.5 py-0.5 text-[10px] ${
                              verdict === "bullish"
                                ? "bg-emerald-500/15 text-emerald-300"
                                : verdict === "bearish"
                                  ? "bg-red-500/15 text-red-300"
                                  : "bg-neutral-500/15 text-muted"
                            }`}
                          >
                            {sym} {verdict}
                          </span>
                        ))}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
