# Brent spike half-retracement study

Tests the idea that after a rapid spike or dip on UK Brent M15, price retraces
about half of the move within a day.

- `spike_lib.py`: loading the MT5 export, adaptive spike detectors (ATR move,
  z-score, range, EMA stretch), retracement statistics, a fade-trade simulator
  that charges each bar's spread, and a random-entry baseline.
- `run_research.py [csv]`: reference test, the 13,056-setting sweep, and
  session and regime breakdowns. Writes `results/summary.json` and `results/sweep.csv`.
- `build_report_data.py`, then `make_report.py`: build `report.html` from the results.

Periods: settings are chosen on 2025-01-02 → 2025-10-07; the last 12 months
(2025-10-08 → 2026-10-08) are the test. The export's server time is UTC.

Requires `pandas` and `numpy`.
