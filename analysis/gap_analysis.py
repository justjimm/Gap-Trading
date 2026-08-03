#!/usr/bin/env python3
"""Recompute weekend-gap P&L under different loss-capping assumptions.

The EA writes one row per weekend to WGH_cycles_<symbol>_<magic>.csv. That row
carries everything needed to answer the question the live journal cannot:
what would this trade have earned if the losing leg's loss had actually been
capped at the amount risked, instead of gapping straight through the stop?

Three P&L series are produced for every capping level:

  realised    what the broker actually paid, stop overshoot included
  capped      the losing leg floored at -cap, the winning leg untouched
  overshoot   capped - realised, i.e. the price of the gap jumping the stop

The `capped` series is not a fantasy. It is what the two-broker structure
delivers: with one account per side, the losing account's downside is bounded
by its own equity through the broker's stop-out and negative balance
protection. Set --cap-mode account to model exactly that.

Usage
-----
    python gap_analysis.py WGH_cycles_XAUUSD_20260803.csv
    python gap_analysis.py cycles.csv --cap-mode account --account-equity 5000
    python gap_analysis.py cycles.csv --sweep
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cycle:
    """One weekend, as recorded by the EA."""

    week_id: int
    symbol: str
    exit_mode: str
    timed_minutes: int
    tp_r: float
    volume: float
    stop_points: float
    risk_money: float
    gap_points: float
    gap_pct: float
    long_profit: float
    short_profit: float
    realised_net: float

    @property
    def legs(self) -> list[float]:
        return [self.long_profit, self.short_profit]

    def capped_net(self, cap: float) -> float:
        """Net P&L with every losing leg floored at -cap.

        A cap of 0 or less means uncapped, which just returns the realised sum.
        """
        if cap <= 0 or not math.isfinite(cap):
            return self.realised_net
        return sum(max(leg, -cap) for leg in self.legs)


def _f(row: dict, key: str, default: float = 0.0) -> float:
    raw = (row.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load(path: Path) -> list[Cycle]:
    cycles: list[Cycle] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("week_id") or "").strip():
                continue
            cycles.append(
                Cycle(
                    week_id=int(_f(row, "week_id")),
                    symbol=(row.get("symbol") or "").strip(),
                    exit_mode=(row.get("exit_mode") or "").strip(),
                    timed_minutes=int(_f(row, "timed_minutes")),
                    tp_r=_f(row, "tp_r"),
                    volume=_f(row, "volume"),
                    stop_points=_f(row, "stop_points"),
                    risk_money=_f(row, "risk_money_per_leg"),
                    gap_points=_f(row, "gap_points"),
                    gap_pct=_f(row, "gap_pct"),
                    long_profit=_f(row, "long_profit"),
                    short_profit=_f(row, "short_profit"),
                    realised_net=_f(row, "realised_net"),
                )
            )
    return cycles


def resolve_cap(cycle: Cycle, mode: str, account_equity: float) -> float:
    """The loss floor to apply to a single leg, in account currency.

    risk     the stop distance as sized at entry. What a stop *would* have
             cost had the market not gapped past it.
    account  the whole account. Models one broker per side: the losing side
             is wiped out and stops there, which is the structural cap.
    none     no floor. Realised P&L.
    """
    if mode == "risk":
        return cycle.risk_money
    if mode == "account":
        return account_equity
    return 0.0


def summarise(cycles: list[Cycle], cap: str, equity: float) -> dict:
    realised = [c.realised_net for c in cycles]
    capped = [c.capped_net(resolve_cap(c, cap, equity)) for c in cycles]
    overshoot = [k - r for k, r in zip(capped, realised)]

    def stats(series: list[float]) -> dict:
        if not series:
            return {"total": 0.0, "mean": 0.0, "wins": 0, "win_rate": 0.0, "worst": 0.0}
        wins = sum(1 for v in series if v > 0)
        return {
            "total": sum(series),
            "mean": sum(series) / len(series),
            "wins": wins,
            "win_rate": wins / len(series) * 100.0,
            "worst": min(series),
        }

    return {
        "n": len(cycles),
        "realised": stats(realised),
        "capped": stats(capped),
        "overshoot_total": sum(overshoot),
        "overshoot_mean": (sum(overshoot) / len(overshoot)) if overshoot else 0.0,
    }


def print_summary(cycles: list[Cycle], cap: str, equity: float) -> None:
    s = summarise(cycles, cap, equity)
    label = {"risk": "capped at risk", "account": "capped at account equity", "none": "uncapped"}[cap]

    print(f"\n  Cycles: {s['n']}   Cap mode: {label}")
    print(f"  {'':16}{'total':>12}{'mean':>12}{'win rate':>11}{'worst':>12}")
    for key in ("realised", "capped"):
        st = s[key]
        print(
            f"  {key:16}{st['total']:>12.2f}{st['mean']:>12.2f}"
            f"{st['win_rate']:>10.1f}%{st['worst']:>12.2f}"
        )
    print(f"\n  Stop overshoot cost: {s['overshoot_total']:.2f} total, "
          f"{s['overshoot_mean']:.2f} per weekend")
    print("  (how much the gap jumping the stop cost versus a stop that held)")


def print_cycles(cycles: list[Cycle], cap: str, equity: float) -> None:
    print(f"\n  {'week':>7}{'gap pts':>10}{'gap %':>8}{'long':>11}"
          f"{'short':>11}{'realised':>11}{'capped':>11}")
    for c in cycles:
        capped = c.capped_net(resolve_cap(c, cap, equity))
        print(
            f"  {c.week_id:>7}{c.gap_points:>10.1f}{c.gap_pct:>8.2f}"
            f"{c.long_profit:>11.2f}{c.short_profit:>11.2f}"
            f"{c.realised_net:>11.2f}{capped:>11.2f}"
        )


def print_by_exit(cycles: list[Cycle], cap: str, equity: float) -> None:
    """Compare the exit variants against each other."""
    groups: dict[tuple, list[Cycle]] = {}
    for c in cycles:
        groups.setdefault((c.exit_mode, c.timed_minutes, c.tp_r), []).append(c)

    if len(groups) < 2:
        return

    print(f"\n  {'exit mode':<18}{'mins':>6}{'tp R':>7}{'n':>5}"
          f"{'realised':>12}{'capped':>12}{'win rate':>11}")
    rows = []
    for (mode, mins, tp), group in groups.items():
        s = summarise(group, cap, equity)
        rows.append((s["capped"]["total"], mode, mins, tp, s))
    for total, mode, mins, tp, s in sorted(rows, reverse=True):
        print(
            f"  {mode:<18}{mins:>6}{tp:>7.1f}{s['n']:>5}"
            f"{s['realised']['total']:>12.2f}{total:>12.2f}"
            f"{s['capped']['win_rate']:>10.1f}%"
        )


def print_sweep(cycles: list[Cycle]) -> None:
    """Show how the edge responds to the size of the loss cap.

    If the strategy only works at caps far below what any stop loss can
    actually deliver, that is the answer: the edge lives in the structural
    cap, not in the order type.
    """
    if not cycles:
        return
    base = sum(c.risk_money for c in cycles) / len(cycles)
    if base <= 0:
        print("\n  Sweep needs risk_money_per_leg in the journal; none recorded.")
        return

    print(f"\n  Loss cap sweep (mean risk per leg = {base:.2f})")
    print(f"  {'cap x risk':>12}{'cap':>12}{'total':>12}{'mean':>12}{'win rate':>11}")
    for mult in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0):
        cap = base * mult
        series = [c.capped_net(cap) for c in cycles]
        wins = sum(1 for v in series if v > 0)
        print(
            f"  {mult:>12.1f}{cap:>12.2f}{sum(series):>12.2f}"
            f"{sum(series) / len(series):>12.2f}{wins / len(series) * 100:>10.1f}%"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="WGH_cycles_*.csv written by the EA")
    ap.add_argument("--cap-mode", choices=("risk", "account", "none"), default="risk",
                    help="what floors a losing leg (default: risk)")
    ap.add_argument("--account-equity", type=float, default=0.0,
                    help="per-account capital, for --cap-mode account")
    ap.add_argument("--sweep", action="store_true", help="sweep the cap level")
    ap.add_argument("--detail", action="store_true", help="print every cycle")
    args = ap.parse_args(argv)

    if not args.csv.exists():
        print(f"No such file: {args.csv}", file=sys.stderr)
        return 1

    cycles = load(args.csv)
    if not cycles:
        print("No completed cycles in the journal yet.", file=sys.stderr)
        return 1

    if args.cap_mode == "account" and args.account_equity <= 0:
        print("--cap-mode account needs --account-equity", file=sys.stderr)
        return 1

    print(f"\n{cycles[0].symbol} - {args.csv.name}")
    print_summary(cycles, args.cap_mode, args.account_equity)
    print_by_exit(cycles, args.cap_mode, args.account_equity)
    if args.sweep:
        print_sweep(cycles)
    if args.detail:
        print_cycles(cycles, args.cap_mode, args.account_equity)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
