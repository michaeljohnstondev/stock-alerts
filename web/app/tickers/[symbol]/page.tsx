import Link from "next/link";
import { notFound } from "next/navigation";
import { api, fmtMoney, fmtPct, gradeColor } from "@/lib/api";
import { ApiDown, Panel } from "@/components/ui";

// Next 15+ hands params in as a Promise.
export default async function TickerPage({
  params,
}: {
  params: Promise<{ symbol: string }>;
}) {
  const { symbol } = await params;

  let t;
  try {
    t = await api.ticker(symbol);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.includes("404")) notFound();
    return <ApiDown error={msg} />;
  }

  const buys = t.ladder.filter((r) => r.direction === "buy");
  const sells = t.ladder.filter((r) => r.direction === "sell");
  const subs = [
    ["Valuation", t.valuation],
    ["Profitability", t.profitability],
    ["Health", t.health],
    ["Growth", t.growth],
  ] as const;

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-xs text-muted hover:text-foreground">
          ← Dashboard
        </Link>
        <div className="mt-2 flex flex-wrap items-baseline gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{t.symbol}</h1>
          <span className="tnum text-xl">{fmtMoney(t.price)}</span>
          {t.grade && (
            <span className={`text-sm font-medium ${gradeColor(t.grade)}`}>
              {t.grade} · {t.value_score}/100
            </span>
          )}
        </div>
        <p className="mt-1 text-sm text-muted">
          {t.sector ?? "—"}
          {t.industry && ` · ${t.industry}`}
          {t.forward_pe != null && ` · fwd P/E ${t.forward_pe.toFixed(1)}`}
          {t.market_cap != null &&
            ` · ${(t.market_cap / 1e9).toFixed(1)}B cap`}
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Ladder title="Buy ladder" rows={buys} side="buy" price={t.price} />
        {sells.length > 0 ? (
          <Ladder title="Sell ladder" rows={sells} side="sell" price={t.price} />
        ) : (
          <Panel title="Sell ladder">
            <p className="px-4 py-6 text-sm text-muted">
              No sell levels set for {t.symbol}.
            </p>
          </Panel>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Value breakdown" hint="25 points each">
          <div className="space-y-3 px-4 py-4">
            {subs.map(([label, v]) => (
              <div key={label} className="flex items-center gap-3">
                <span className="w-24 text-sm text-muted">{label}</span>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-neutral-800">
                  <div
                    className="h-full rounded-full bg-sky-500/70"
                    style={{ width: `${((v ?? 0) / 25) * 100}%` }}
                  />
                </div>
                <span className="tnum w-10 text-right text-sm">{v ?? "—"}</span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel
          title="Price history"
          hint={`${t.price_history.length} observation${t.price_history.length === 1 ? "" : "s"}`}
        >
          {t.price_history.length < 2 ? (
            <p className="px-4 py-6 text-sm text-muted">
              Only one observation so far. A trend line needs several scans —
              this fills in as you keep running the scanner.
            </p>
          ) : (
            <Sparkline points={t.price_history.map((p) => p.price)} />
          )}
        </Panel>
      </div>
    </div>
  );
}

function Ladder({
  title,
  rows,
  side,
  price,
}: {
  title: string;
  rows: Awaited<ReturnType<typeof api.ticker>>["ladder"];
  side: "buy" | "sell";
  price: number | null;
}) {
  return (
    <Panel title={title} hint={side === "buy" ? "support" : "resistance"}>
      <table className="w-full text-sm">
        <tbody>
          {rows.map((r) => (
            <tr key={r.rung} className="border-b border-edge/60 last:border-0">
              <td className="px-4 py-2 text-muted">rung {r.rung}</td>
              <td className="tnum px-2 py-2 text-right">{fmtMoney(r.price)}</td>
              <td
                className={`tnum px-2 py-2 text-right ${
                  r.pct_away != null && r.pct_away < 0
                    ? "text-emerald-400"
                    : "text-muted"
                }`}
              >
                {fmtPct(r.pct_away)}
              </td>
              <td className="px-4 py-2 text-right">
                {r.triggered ? (
                  <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300">
                    triggered
                  </span>
                ) : (
                  <span className="text-xs text-muted">{r.source}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {price != null && (
        <p className="border-t border-edge px-4 py-2 text-xs text-muted">
          Current price <span className="tnum">{fmtMoney(price)}</span>
        </p>
      )}
    </Panel>
  );
}

/** Inline SVG — no chart library for a single series. */
function Sparkline({ points }: { points: number[] }) {
  const w = 600;
  const h = 120;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p - min) / span) * h;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <div className="px-4 py-4">
      <svg viewBox={`0 0 ${w} ${h}`} className="h-32 w-full" preserveAspectRatio="none">
        <path d={d} fill="none" stroke="currentColor" strokeWidth="2" className="text-sky-400" />
      </svg>
      <div className="tnum mt-2 flex justify-between text-xs text-muted">
        <span>{min.toFixed(2)}</span>
        <span>{max.toFixed(2)}</span>
      </div>
    </div>
  );
}
