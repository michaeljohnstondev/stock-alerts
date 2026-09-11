import { api, fmtPct, gradeColor } from "@/lib/api";
import { AlertsTable, ApiDown, Panel, Stat } from "@/components/ui";

// Server component: these fetches run on the server, so the browser
// never talks to FastAPI directly and the API URL stays server-side.
export default async function Dashboard() {
  let data;
  try {
    // Promise.all, not four awaits in a row -- the requests are
    // independent, so they should overlap rather than queue.
    const [stats, alerts, grades, sectors, runs] = await Promise.all([
      api.stats(),
      api.alerts({ direction: "buy", minRung: 2, limit: 40 }),
      api.grades(),
      api.sectors(),
      api.runs(1),
    ]);
    data = { stats, alerts, grades, sectors, runs };
  } catch (e) {
    return <ApiDown error={e instanceof Error ? e.message : String(e)} />;
  }

  const { stats, alerts, grades, sectors, runs } = data;
  const lastRun = runs[0];
  const escalated = alerts.filter((a) => a.escalated).length;
  const totalGraded = grades.reduce((n, g) => n + g.names, 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
        <p className="mt-1 text-sm text-muted">
          {stats.latest_run_at
            ? `Last scan ${new Date(stats.latest_run_at).toLocaleString()}`
            : "No scans recorded yet"}
          {lastRun?.duration_seconds != null &&
            ` · ${Math.round(lastRun.duration_seconds)}s · ${lastRun.api_calls} API calls`}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Triggered buys" value={alerts.length} sub={`${escalated} deeper than last run`} />
        <Stat label="Rated tickers" value={stats.tickers} sub={`${stats.rating_levels} rungs`} />
        <Stat label="Scans recorded" value={stats.runs} sub={`${stats.history_days}d of history`} />
        <Stat label="Alert events" value={stats.alert_events.toLocaleString()} sub="every rung, every run" />
      </div>

      {stats.runs < 3 && (
        <div className="rounded-lg border border-sky-500/25 bg-sky-500/5 px-4 py-3 text-sm">
          <span className="text-sky-300">History is still building.</span>{" "}
          <span className="text-muted">
            Trend and post-trigger analytics need several runs — keep scanning
            and they fill in on their own.
          </span>
        </div>
      )}

      <Panel
        title="Triggered buy rungs"
        hint={`rung 2+ · latest run`}
      >
        <AlertsTable alerts={alerts} />
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Sector rotation" hint="change vs previous snapshot">
          <table className="w-full text-sm">
            <tbody>
              {sectors.map((s) => (
                <tr key={s.sector} className="border-b border-edge/60 last:border-0">
                  <td className="tnum w-10 px-4 py-2 text-muted">{s.rank}</td>
                  <td className="px-2 py-2">{s.sector}</td>
                  <td className="tnum px-2 py-2 text-right">{s.composite.toFixed(1)}</td>
                  <td className="px-2 py-2 text-right">
                    <span
                      className={
                        s.status === "leading"
                          ? "text-emerald-400"
                          : s.status === "lagging"
                            ? "text-red-400"
                            : "text-muted"
                      }
                    >
                      {s.status}
                    </span>
                  </td>
                  <td className="tnum w-14 px-4 py-2 text-right">
                    {s.rank_change == null || s.rank_change === 0 ? (
                      <span className="text-muted">—</span>
                    ) : (
                      <span className={s.rank_change > 0 ? "text-emerald-400" : "text-red-400"}>
                        {s.rank_change > 0 ? "▲" : "▼"} {Math.abs(s.rank_change)}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Value grades" hint={`${totalGraded} names scored`}>
          <div className="space-y-2 px-4 py-3">
            {grades.map((g) => (
              <div key={g.grade} className="flex items-center gap-3">
                <span className={`tnum w-8 text-sm font-medium ${gradeColor(g.grade)}`}>
                  {g.grade}
                </span>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-neutral-800">
                  <div
                    className="h-full rounded-full bg-sky-500/70"
                    style={{ width: `${(g.names / totalGraded) * 100}%` }}
                  />
                </div>
                <span className="tnum w-10 text-right text-xs text-muted">{g.names}</span>
                <span className="tnum w-12 text-right text-xs text-muted">
                  {g.avg_score?.toFixed(1) ?? "—"}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
