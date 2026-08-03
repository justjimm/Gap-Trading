# Weekend Gap Trading

An MT5 expert advisor and analysis harness for trading the gap between the
Friday close and the Sunday/Monday reopen on XAUUSD and UK Brent.

## The problem this is built to measure

Open a max-size buy and a max-size sell on the same symbol, hold them over the
weekend, close both together: the gap cancels exactly. The long gains whatever
the short loses. Net result is minus two spreads and swap, every time,
regardless of how large the gap is.

So the gap only pays if the two legs are treated **asymmetrically** — the loser
is cut short and the winner is allowed to run. The obvious way to do that is a
stop loss on each leg.

**A stop loss cannot do it.** An MT5 stop is a resting market order. When price
gaps past it over the weekend, it fills at the first available price, not at the
stop price. Realised loss is `intended_risk + gap_overshoot`, and the overshoot
is unbounded — precisely on the weekends where the gap is large enough to be
worth trading. The winning leg captures the gap; the losing leg pays the gap
*plus* the risk. That is the trap this repository exists to quantify.

### What actually caps the loss

Running the two sides on **two accounts at two brokers** — one max long, one max
short. Each account's downside is then bounded by its own equity through the
broker's stop-out and negative balance protection. The losing account is wiped
out at a known number and stops there; the winning account runs unbounded. The
payoff becomes a synthetic long straddle, where the cap is structural rather
than an order type the market can jump over.

The EA supports both shapes so you can measure the difference:

| `InpLegMode` | Deployment | Loss cap |
|---|---|---|
| `LEG_BOTH` | Both legs, one hedging account | Stop loss only — gaps through it |
| `LEG_LONG` | Long only, broker A | Account equity (structural) |
| `LEG_SHORT` | Short only, broker B | Account equity (structural) |

`LEG_BOTH` is the probe. It needs a hedging account and the EA refuses to start
on a netting one, where a buy and a sell would collapse into a single position.
`LEG_LONG` / `LEG_SHORT` have no such requirement.

## Answering "what if the loss had been capped?"

Both in the EA and afterwards from the data.

**In the EA.** At entry it records the stop distance and converts it to account
currency via `OrderCalcProfit`, giving `risk_money_per_leg`. When the cycle
closes it writes both the realised net and the counterfactual
`capped_net = Σ max(leg_pnl, -risk_money)`, plus `overshoot_cost`, the
difference between them. That last column is the cost of the gap jumping the
stop, isolated as a single number per weekend.

**Afterwards.** `analysis/gap_analysis.py` reloads the journal and recomputes
under any capping rule, so you are not locked into the one the EA assumed:

```bash
# what a stop that actually held would have produced
python analysis/gap_analysis.py WGH_cycles_XAUUSD_20260803.csv

# the two-broker structure: each side capped at its own account equity
python analysis/gap_analysis.py cycles.csv --cap-mode account --account-equity 5000

# how the edge responds to cap width, and a comparison of the exit variants
python analysis/gap_analysis.py cycles.csv --sweep --detail
```

The sweep is the diagnostic that matters. If the strategy is only profitable at
caps far tighter than any stop loss can deliver in practice, that tells you the
edge lives entirely in the structural cap, and the single-account version should
be abandoned rather than tuned.

## Entry

The EA finds the weekly close from the broker's own session table
(`SymbolInfoSessionTrade`) rather than assuming a Friday time — Brent and gold
close at different hours and brokers differ. A day is the last trading day of
the week when it has sessions and the following day has none. Entry fires in the
final `InpEntrySeconds` (default 30) before that close, stopping
`InpEntryDeadline` seconds early so the order has time to reach the server.

Sizing walks up in whole `SYMBOL_VOLUME_STEP` increments and verifies each
candidate with `OrderCalcMargin`, stepping down until one fits — tiered margin
schedules make the naive `free_margin / margin_per_lot` estimate too optimistic.
`InpMarginBufferPct` (default 80%) leaves headroom; at 100% the margin level sits
at the stop-out threshold from the first tick.

## Exits

`InpExitMode`:

- `EXIT_TIMED` — flatten `InpTimedExitMinutes` after the reopen (5, 15, 60, 120, 240)
- `EXIT_R_MULTIPLE` — take profit at `InpTakeProfitR` × the risked distance
- `EXIT_HYBRID` — the R-multiple target, a timed backstop, and a move to
  breakeven once the winner clears `InpBreakEvenR`

`InpLoserMode` controls the losing leg on `LEG_BOTH`: leave it to the stop
(`LOSER_STOP_LOSS`), market-close it on the first tick after the reopen
(`LOSER_CLOSE_OPEN`), or hold both to exit together as a symmetric baseline
(`LOSER_HOLD_BOTH`).

The reopen is detected from a discontinuity between consecutive tick times
(≥ 15 minutes), which works identically live and in the Strategy Tester.

## Journal

Written to the terminal's **common** files folder
(`File > Open Data Folder`, then up to `Common\Files`):

- `WGH_cycles_<symbol>_<magic>.csv` — one row per weekend, analysis-ready
- `WGH_deals_<symbol>_<magic>.csv` — every deal, with `DEAL_REASON` so you can
  see whether an exit was a stop loss, a take profit, a stop-out or the EA

Cycle state is mirrored into terminal global variables, so a restart over the
weekend does not lose the open cycle's journal row.

## Install

Copy `MQL5/Experts/WeekendGapHedge/WeekendGapHedge.mq5` into the terminal's
`MQL5/Experts` folder, compile in MetaEditor (F7), and attach to an XAUUSD or
Brent chart with algo trading enabled. Give each symbol its own `InpMagic` so
the journals and position filters stay separate.

## Status

This is a measurement instrument, not a finished strategy. Run `LEG_BOTH` first
to size the overshoot cost on real data, then decide whether the two-broker
build is worth standing up.

### Not yet built

- Coordination between the two broker accounts. Each runs independently today;
  there is no check that both sides actually filled, and a one-sided fill leaves
  a naked max-leverage position over the weekend.
- Broker-specific Brent symbol naming (`UKOIL`, `BRENT`, `XBRUSD` — varies).
- Slippage assumptions in the Strategy Tester are optimistic about weekend gaps;
  treat backtested overshoot as a lower bound on the real thing.
