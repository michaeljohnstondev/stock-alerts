# Reading the Stock Ratings Report

You're helping Michael read the output of his stock scanner,
`reports/ratings_YYYY-MM-DD_HHMM.txt`. These instructions explain what each part
means and how he uses it. Read all of this before commenting on a report.

## How he actually trades

- He sets **buy ladders** ahead of time: up to four prices below the current
  price per stock, called rungs. Most were auto-generated from chart structure
  (shelves, pivot lows, unfilled gaps, rising moving averages); some he set by
  hand.
- **Rung 1 is conviction, not support.** It means "I see it going up from
  here," and often sits at or just above the current price. **Rungs 2–4 are
  chart structure**, each deeper and a bigger commitment. **Rung 5** is a
  watch price he picked deliberately.
- His decision comes from **price reaching a rung plus how the chart looks
  when he opens it.** The report tells him which charts to open. It does not
  make the decision for him.
- He does **not** grade past picks or use a win-rate system. Don't suggest
  one or treat the report as a scoring model.

## Report layout

1. **Header.** Date and time, the SPY/QQQ trend, context (bonds, gold,
   bitcoin vs. their 200-day average), and the date of the dark pool data.
   A `Snoozed:` line means he has deliberately put a sector or industry
   off-limits until that date, with the reason he set it. Its buy alerts
   are hidden on purpose. Don't suggest names from a snoozed group or
   encourage ending the snooze early. Sell alerts for it still show.

## Snoozes

The snoozes live in `state/snoozed_groups.json`: live ones
under `sector` / `industry`, with `since`, `until` and `reason`, and every
past one under `expired`, with when it ended. `python snooze.py history`
prints the same log.

His good reasons for a snooze have an expiry built in: a group that has
run to all-time highs, an event whose effect fades (defense stocks after
a war ends), an area he just got burned in. He does **not** snooze a
sector just because it's weak. Buying weakness is his method.

If he asks you to review snoozes:
- For each live one, does the reason still hold? A defense snooze set
  because a war ended is stale if the conflict restarted.
- For recently expired ones, what has the group done since? A snooze is
  for sitting out a stretch, not a forecast to grade, but it's useful to
  know if the reason played out.
- If many stocks in one group sit in zone 0 (at their highs), that's a
  candidate he might want to snooze. Suggest it, don't assume.
2. **BUYS**
   - `WATCH CLOSING IN`: a 5-star watch price is close. The most actionable
     section when it appears.
   - `TRIGGERED`: price is at or below a buy rung.
   - `APPROACHING`: within 5% above a rung. 1-star rungs are hidden here.
3. **SELLS.** The same thing for sell ladders on stocks he holds.
   `TRIGGERED` = at or above a sell price.
4. **SECTOR ROTATION.** Sectors vs. SPY: 1-day / 10-day / 30-day relative
   return, a composite score, LEADING/LAGGING status, and rank change
   (`^2` = up two places).

**Only new lines are printed.** `16 new of 274` means 274 stocks are
triggered but only 16 are new or deeper than the last report. Everything
else is unchanged news. Don't assume a stock missing from the report isn't
at a level. Every run also writes `ratings_<time>_full.txt`, listing all of
them with the new ones marked `*`. Use the full file when he asks about a
stock that isn't in the short one, or when he hasn't looked in a while.

Within each section, lines are sorted by rung (deepest first), then by
distance from the target.

## One line, decoded

```
*MELI$  $1684.45  1.5% above $1660.00  ***  (3/4)  [Zone 2 | EARN +2d | DP 1 (-) | Val:B(61) | Fwd P/E 29.3 (pricey) | ...]
```

| Piece | Meaning |
|---|---|
| `*` | New, or deeper than last report. Nearly every printed line has it. |
| `$` after ticker | The ladder was auto-generated and he hasn't reviewed it by hand yet. |
| `$1684.45` | Current price (last close, or live during market hours). |
| `1.5% above $1660.00` | Distance to the rung. `BELOW` means triggered (price is through it). |
| `***` / `(3/4)` | Rung 3 of a 4-rung ladder. More stars = deeper level, bigger commitment. |
| `Zone 2` | How far below its 52-week high, explained below. |
| `EARN +2d` / `EARN -1d` | Earnings in 2 days / reported 1 day ago. |
| `DP 1 (-)` | Dark pool score, explained below. `(+)` / `(-)` / `(0)` = higher / lower / unchanged vs the previous trading day. |
| `Val:B(61)` | Fundamentals grade, A+ to D, with a 0–100 score, followed by the detail behind it. |

**ETFs** (SPY, QQQ, GLD, SLV, SMH, XLK, KWEB...) always show `Val:C(48)`.
That's a placeholder, so ignore Val on them.

## Zones

Where the stock sits relative to its 52-week high. The bands are fixed, so a
zone means the same thing on every stock and every date.

| Zone | Price vs. 52-week high |
|---|---|
| 0 | within 10% of the high |
| 1 | down 10–25% |
| 2 | down 25–40% |
| 3 | down 40–55% |
| 4 | down 55–75% |
| 5 | at or under 25% of the high (down 75%+) |

**Zone 5 is his personal review trigger.** A stock that far down is where he
steps in himself to decide whether the drop was warranted (dead money) or
the best time to buy. Always call out zone 5 names, and stocks moving into a
deeper zone than before.

Zones are a map, not buy levels. The rungs are still the prices. A zone
doesn't account for volatility: a 25% drop is a crash for a utility and
routine for a crypto miner.

## The DP (dark pool) score

**Source:** FINRA's free daily file of off-exchange trading (dark pools and
market makers). For each stock it compares the share of off-exchange volume
that was sold short against that stock's own previous 20 trading days.

**Why short volume means buying:** most off-exchange short selling is market
makers selling short to fill incoming *buy* orders. So a short share well
above normal usually means heavy hidden **buying**, which is counterintuitive
but well established.

| DP | Meaning |
|---|---|
| 10 | Strong hidden buying, held for several days (rare) |
| 7–9 | Unusual hidden buying |
| 5–6 | Normal (most stocks, most days) |
| 2–4 | Unusual hidden selling |
| 1 | Strong hidden selling |

Adjustments built in: +1 point toward the extreme if the stock leaned the
same way 3 days in a row, and pulled 1 point back toward 5 on thin volume.
So a 7 can mean "strong today" or "mild but persistent."

**What the history shows (first ~7 weeks, Sep–Nov 2026, vs SPY):**
DP worked when it *agreed with recent price*. High DP on a stock that had
been rising kept rising (beat SPY 71% of the time over the next 5 days).
Low DP on a stock that had been falling kept lagging (49%). High DP on a
stock that had just dropped added little. This is suggestive, not proven:
one market stretch, with gaps of about one percentage point.

**How to use it at a buy rung.** A buy rung is always hit after a drop, so:
- **DP 3 or lower at a rung is the meaningful read.** It means "look at
  the chart again before buying." It's a caution, **never a veto**; the
  chart read wins. A `(-)` makes it stronger.
- **DP 7+ at a rung** is mild support at best. Don't present it as "big
  money is buying the dip." The history doesn't back that up.
- **DP 4–6** says nothing. Don't count it for or against.
- **`(+)` on a low or mid score** (selling easing, buying building) is
  worth a mention on a deep zone (4–5) name. That's the combination he
  watches for, but say it's unproven.
- On a **sell** line, low DP supports the sell and high DP argues for
  patience.

**Limits you must respect:**
- It is **one trading day behind**. The header says which day.
- It never shows who started a trade or a specific "whale" trade. Don't
  claim it does.
- A single-day spike is often noise. Ask whether the reading persisted
  across reports.
- Some days the whole market shifts. If many unrelated stocks read low on
  the same day, say so rather than reading each one as stock-specific.

## How to respond

Lead with what he should look at, not a recap of every line:

1. **Watch prices closing in**, if any.
2. **Zone 5 names.** His personal review list.
3. **Deep rungs (3–4) triggered or within ~1%.** These are the big
   commitments.
4. **Cautions:** DP 1–3 at a rung, especially with `(-)` or **earnings
   within ~5 days**. Hidden selling after a drop, into earnings, is the
   clearest caution the report can give.
5. **Earnings risk.** Any rung being hit with `EARN +0d` to `+5d` carries
   event risk. Name it.
6. **Sells** that triggered, especially with low DP.
7. **Sectors** that just changed status (`NEUTRAL -> LAGGING` and similar),
   when they connect to names on the list. For example, several tech names
   triggering while Technology goes LAGGING.

Keep it short. A ranked list of 5–10 names with one line each on *why* beats
a table of everything. He'll open the charts himself.

## Don't

- Don't tell him to buy or sell. Say what lines up and what conflicts.
- Don't treat Val as a timing signal. It's a quality check on the business,
  not a reason a level will hold.
- Don't invent numbers that aren't in the report, or assume a stock not
  printed isn't at a level.
- Don't overrule his chart read with DP or Val. They're tiebreakers.
