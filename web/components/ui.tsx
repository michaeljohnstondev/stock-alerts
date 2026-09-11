import Link from "next/link";
import { Alert, fmtMoney, fmtPct, gradeColor } from "@/lib/api";

export function Panel({
  title,
  hint,
  children,
  className = "",
}: {
  title?: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-edge bg-panel ${className}`}
    >
      {title && (
        <div className="flex items-baseline justify-between border-b border-edge px-4 py-3">
          <h2 className="text-sm font-medium">{title}</h2>
          {hint && <span className="text-xs text-muted">{hint}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <div className="rounded-lg border border-edge bg-panel px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="tnum mt-1 text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

/** Rung depth as filled pips — reads faster than a number in a table. */
export function RungPips({ rung, total = 4 }: { rung: number; total?: number }) {
  const n = Math.max(total, rung);
  return (
    <span className="inline-flex items-center gap-0.5" title={`rung ${rung}`}>
      {Array.from({ length: n }, (_, i) => (
        <span
          key={i}
          className={`h-1.5 w-1.5 rounded-full ${
            i < rung ? "bg-sky-400" : "bg-neutral-700"
          }`}
        />
      ))}
      <span className="tnum ml-1 text-xs text-muted">{rung}</span>
    </span>
  );
}

export function AlertsTable({ alerts }: { alerts: Alert[] }) {
  if (alerts.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-muted">
        Nothing triggered in the latest run.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-edge text-left text-xs text-muted">
            <th className="px-4 py-2 font-medium">Symbol</th>
            <th className="px-4 py-2 font-medium">Sector</th>
            <th className="px-4 py-2 font-medium">Rung</th>
            <th className="px-4 py-2 text-right font-medium">Target</th>
            <th className="px-4 py-2 text-right font-medium">Price</th>
            <th className="px-4 py-2 text-right font-medium">Through</th>
            <th className="px-4 py-2 text-right font-medium">Grade</th>
          </tr>
        </thead>
        <tbody>
          {alerts.map((a) => (
            <tr
              key={`${a.symbol}-${a.direction}-${a.rung}`}
              className="border-b border-edge/60 last:border-0 hover:bg-white/[0.02]"
            >
              <td className="px-4 py-2">
                <Link
                  href={`/tickers/${a.symbol}`}
                  className="font-medium hover:underline"
                >
                  {a.symbol}
                </Link>
                {a.escalated && (
                  <span className="ml-2 rounded bg-sky-500/15 px-1.5 py-0.5 text-[10px] text-sky-300">
                    deeper
                  </span>
                )}
              </td>
              <td className="px-4 py-2 text-muted">{a.sector ?? "—"}</td>
              <td className="px-4 py-2">
                <RungPips rung={a.rung} />
              </td>
              <td className="tnum px-4 py-2 text-right text-muted">
                {fmtMoney(a.target_price)}
              </td>
              <td className="tnum px-4 py-2 text-right">{fmtMoney(a.price)}</td>
              <td
                className={`tnum px-4 py-2 text-right ${
                  a.pct_away < 0 ? "text-emerald-400" : "text-muted"
                }`}
              >
                {fmtPct(a.pct_away)}
              </td>
              <td className="px-4 py-2 text-right">
                <span className={`font-medium ${gradeColor(a.grade)}`}>
                  {a.grade ?? "—"}
                </span>
                {a.value_score != null && (
                  <span className="tnum ml-1.5 text-xs text-muted">
                    {a.value_score}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Shown when the API isn't reachable, instead of a stack trace. */
export function ApiDown({ error }: { error: string }) {
  return (
    <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-6">
      <h2 className="text-sm font-medium text-amber-300">API unreachable</h2>
      <p className="mt-1 text-sm text-muted">
        Start it with{" "}
        <code className="rounded bg-black/30 px-1.5 py-0.5 text-xs">
          uvicorn api.main:app --reload
        </code>{" "}
        from the project root.
      </p>
      <p className="mt-2 text-xs text-muted">{error}</p>
    </div>
  );
}
