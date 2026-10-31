"""Run the spike half-retracement study and write results to results/.

Usage:
    python run_research.py [path/to/MT5_M15.csv]

Periods
- IS  (in-sample, used to pick parameters): first bar .. 2025-10-07
- OOS (out-of-sample, the "last 12 months"): 2025-10-08 .. last bar
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spike_lib import (Detector, detect, is_eia_bar, load_mt5_csv,  # noqa: E402
                       random_baseline, retracement_stats, session_label,
                       simulate, summarise)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "..", "..", "data", "UK_Brent_Oil_M15_202501020100_202610080000.csv")
OUT = os.path.join(HERE, "results")
OOS_START = "2025-10-08"

# The plain-English hypothesis, fixed before looking at results:
# a move of >= 3x the recent average bar range within 4 bars (1 hour),
# measured against the previous 8 hours; target half the move back.
REFERENCE = Detector("atr_move", n=4, m=32, k=3.0)
REF_EXIT = dict(f_target=0.5, stop_atr=1.0, time_stop=96, entry="next_open")


def detectors():
    for n, m, k in itertools.product((1, 2, 4, 8), (16, 32, 96), (2.5, 3, 4, 5, 6)):
        yield Detector("atr_move", n, m, k)
    for n, m, k in itertools.product((1, 2, 4, 8), (96, 192, 384), (2.5, 3, 4, 5)):
        yield Detector("zscore", n, m, k)
    for m, k in itertools.product((16, 32, 96), (2, 3, 4, 5)):
        yield Detector("range", 1, m, k)
    for n, m, k in itertools.product((1, 4), (32, 96), (2, 3, 4, 5)):
        yield Detector("ema_stretch", n, m, k)


EXITS = [dict(f_target=f, stop_atr=s, time_stop=ts, entry=e)
         for f, s, ts, e in itertools.product((0.25, 0.382, 0.5, 0.618), (0.5, 1, 2, 3),
                                              (24, 48, 96), ("next_open", "confirm"))]


def split(ev, cut):
    return ev[ev["i"] < cut], ev[ev["i"] >= cut]


def main(csv):
    os.makedirs(OUT, exist_ok=True)
    df = load_mt5_csv(csv)
    cut = int(df.index[df["t"] >= OOS_START][0])
    n = len(df)
    df["session"] = session_label(df["t"], df["us_summer"])
    df["eia"] = is_eia_bar(df["t"], df["us_summer"])
    meta = {
        "bars": n, "first": str(df["t"].iloc[0]), "last": str(df["t"].iloc[-1]),
        "is_range": [str(df["t"].iloc[0]), str(df["t"].iloc[cut - 1])],
        "oos_range": [str(df["t"].iloc[cut]), str(df["t"].iloc[-1])],
        "median_spread_pts": float(df["spread"].median()),
        "median_bar_range": float((df["high"] - df["low"]).median()),
        "price_min": float(df["low"].min()), "price_max": float(df["high"].max()),
    }

    # ---------------------------------------------------------------- 1. merit
    ev = detect(df, REFERENCE)
    merit = {}
    for name, e, lo, hi in (("IS", *split(ev, cut)[:1], 0, cut), ("OOS", split(ev, cut)[1], cut, n)):
        rs = retracement_stats(df, e)
        tr = simulate(df, e, **REF_EXIT)
        bl = random_baseline(df, tr, e, lo, hi, reps=200)
        merit[name] = {
            "events": int(len(e)),
            "events_per_week": float(len(e) / (df.loc[lo:hi - 1, "week"].nunique())),
            "median_move": float(e["move"].median()),
            "median_move_atr": float(e["move_atr"].median()),
            "retrace_hit": {f: float((rs["max_retrace"] >= f).mean()) for f in (0.25, 0.382, 0.5, 0.618, 1.0)},
            "median_bars_to": {f: float(rs[f"bars_to_{f:g}"].median()) for f in (0.25, 0.382, 0.5, 0.618, 1.0)},
            "p75_bars_to_50": float(rs["bars_to_0.5"].quantile(0.75)),
            "continued_first": float(rs["continued_first"].mean()),
            "trade": summarise(tr),
            "baseline": bl,
        }
        rs.to_csv(os.path.join(OUT, f"reference_events_{name}.csv"), index=False)
        tr.to_csv(os.path.join(OUT, f"reference_trades_{name}.csv"), index=False)
        if name == "OOS":
            # Retracement-depth histogram (fraction of move reached at best within a day)
            bins = [0, .25, .382, .5, .618, 1.0, 1.5, np.inf]
            merit["depth_hist_OOS"] = pd.cut(rs["max_retrace"], bins, right=False).value_counts(sort=False).astype(int).to_dict()
            merit["depth_hist_OOS"] = {str(k): v for k, v in merit["depth_hist_OOS"].items()}
            # Retracement hit-rate curve vs horizon
            merit["hit50_by_horizon_OOS"] = {h: float((rs["bars_to_0.5"] <= h).mean()) for h in (1, 2, 4, 8, 16, 32, 64, 96)}

    # Null for the descriptive stat: same-size moves measured from random bars,
    # direction "back towards where price was N bars ago".
    rng = np.random.default_rng(1)
    null = {}
    for name, lo, hi in (("IS", REFERENCE.n + REFERENCE.m, cut), ("OOS", cut, n - 2)):
        e = ev[(ev["i"] >= lo) & (ev["i"] < hi)]
        cand = rng.integers(lo, hi, size=len(e) * 3)
        c = df["close"].to_numpy()
        hh = df["high"].rolling(REFERENCE.n).max().to_numpy()
        ll = df["low"].rolling(REFERENCE.n).min().to_numpy()
        base = df["close"].shift(REFERENCE.n).to_numpy()
        up = c[cand] >= base[cand]
        fake = pd.DataFrame({"i": cand, "side": np.where(up, 1, -1),
                             "extreme": np.where(up, hh[cand], ll[cand]), "base": base[cand]})
        fake["move"] = (fake["extreme"] - fake["base"]).abs()
        # rescale to the event move sizes in ATR units so the race has equal geometry
        ctx = (df["high"] - df["low"]).rolling(REFERENCE.m).mean().shift(REFERENCE.n).to_numpy()
        sizes = rng.choice(e["move_atr"].to_numpy(), size=len(fake))
        fake["move"] = sizes * ctx[cand]
        fake["base"] = fake["extreme"] - fake["side"] * fake["move"]
        fake = fake[np.isfinite(fake["move"]) & (fake["move"] > 0)].head(len(e) * 2)
        rs = retracement_stats(df, fake)
        null[name] = {"retrace50": float((rs["max_retrace"] >= 0.5).mean()),
                      "continued_first": float(rs["continued_first"].mean()),
                      "median_bars_to_50": float(rs["bars_to_0.5"].median())}
    merit["null_random_moves"] = null

    # ---------------------------------------------------------------- 3. sweep
    rows = []
    t0 = time.time()
    for d in detectors():
        ev = detect(df, d)
        e_is, e_oos = split(ev, cut)
        for ex in EXITS:
            r = {"kind": d.kind, "N": d.n, "M": d.m, "k": d.k, **ex}
            for name, e in (("IS", e_is), ("OOS", e_oos)):
                s = summarise(simulate(df, e, **ex))
                for key in ("trades", "win", "avgR", "pf", "totalR", "pnl_pts"):
                    r[f"{name}_{key}"] = s[key]
            rows.append(r)
    sweep = pd.DataFrame(rows)
    sweep.to_csv(os.path.join(OUT, "sweep.csv"), index=False)
    print(f"sweep: {len(sweep)} configs in {time.time() - t0:.0f}s")

    # Selection honesty: pick the best IS configs (min 40 IS trades), see OOS.
    s = sweep[(sweep["IS_trades"] >= 40) & (sweep["OOS_trades"] >= 40)].copy()
    top = s.sort_values("IS_avgR", ascending=False).head(20)
    robust = {
        "configs_tested": int(len(sweep)),
        "configs_with_40_trades": int(len(s)),
        "share_IS_positive": float((s["IS_avgR"] > 0).mean()),
        "share_OOS_positive": float((s["OOS_avgR"] > 0).mean()),
        "share_both_positive": float(((s["IS_avgR"] > 0) & (s["OOS_avgR"] > 0)).mean()),
        "corr_IS_OOS_avgR": float(s["IS_avgR"].corr(s["OOS_avgR"])),
        "top20_by_IS": top.to_dict(orient="records"),
        "top20_mean_IS_avgR": float(top["IS_avgR"].mean()),
        "top20_mean_OOS_avgR": float(top["OOS_avgR"].mean()),
    }
    # Marginal effects (mean OOS and IS avgR by each parameter value)
    marg = {}
    for col in ("kind", "N", "M", "k", "f_target", "stop_atr", "time_stop", "entry"):
        g = s.groupby(col)[["IS_avgR", "OOS_avgR"]].mean()
        marg[col] = {str(i): {"IS": float(v.IS_avgR), "OOS": float(v.OOS_avgR)} for i, v in g.iterrows()}
    robust["marginal"] = marg
    # Heatmap: atr_move, f=0.5, time=96, next_open; k x stop, averaged over N, M
    hm = s[(s["kind"] == "atr_move") & (s["f_target"] == 0.5) & (s["time_stop"] == 96)]
    for name in ("IS", "OOS"):
        for ent in ("next_open", "confirm"):
            p = hm[hm["entry"] == ent].pivot_table(index="k", columns="stop_atr", values=f"{name}_avgR", aggfunc="mean")
            robust[f"heat_{name}_{ent}"] = {"rows": [float(x) for x in p.index], "cols": [float(x) for x in p.columns],
                                            "values": p.round(4).values.tolist()}
    # Heatmap k x f (stop 2 ATR, time 96, confirm)
    for name in ("IS", "OOS"):
        p = s[(s["kind"] == "atr_move") & (s["stop_atr"] == 2) & (s["time_stop"] == 96) & (s["entry"] == "confirm")] \
            .pivot_table(index="k", columns="f_target", values=f"{name}_avgR", aggfunc="mean")
        robust[f"heatKF_{name}"] = {"rows": [float(x) for x in p.index], "cols": [float(x) for x in p.columns],
                                    "values": p.round(4).values.tolist()}

    # Baselines for the 5 best IS configs, evaluated OOS
    best = []
    for r in top.head(5).to_dict(orient="records"):
        d = Detector(r["kind"], int(r["N"]), int(r["M"]), float(r["k"]))
        ex = {k: r[k] for k in ("f_target", "stop_atr", "time_stop", "entry")}
        ex["time_stop"] = int(ex["time_stop"])
        e_oos = split(detect(df, d), cut)[1]
        tr = simulate(df, e_oos, **ex)
        bl = random_baseline(df, tr, e_oos, cut, n, reps=200, time_stop=ex["time_stop"])
        tr2 = simulate(df, e_oos, **ex, cost_mult=0.0)
        best.append({"label": d.label, **ex, "OOS": summarise(tr), "OOS_no_cost": summarise(tr2), "baseline": bl})
    robust["best5_oos_vs_random"] = best

    # ---------------------------------------------------------------- sessions / regime
    ev = detect(df, REFERENCE)
    gross = []
    for name, e in zip(("IS", "OOS"), split(ev, cut)):
        tr = simulate(df, e, **REF_EXIT, non_overlap=False)
        tr0 = simulate(df, e, **REF_EXIT, non_overlap=False, cost_mult=0.0)
        tr["R_gross"] = tr0["R"].to_numpy()
        tr["period"] = name
        gross.append(tr)
    tr = pd.concat(gross, ignore_index=True)
    sig = tr["sig_i"].to_numpy()
    tr["session"] = df["session"].to_numpy()[sig]
    tr["eia"] = df["eia"].to_numpy()[sig] | df["eia"].to_numpy()[np.maximum(sig - 1, 0)]
    first_of_week = df["week"].diff().fillna(1).to_numpy() > 0
    fw = pd.Series(first_of_week).rolling(REFERENCE.n + 1, min_periods=1).max().to_numpy() > 0
    tr["week_open"] = fw[sig]
    lr = pd.Series(df["high"] - df["low"]).rolling(96 * 20, min_periods=96 * 5).median().to_numpy()
    ctx = (df["high"] - df["low"]).rolling(REFERENCE.m).mean().shift(REFERENCE.n).to_numpy()
    tr["vol_regime"] = pd.qcut(ctx[sig] / lr[sig], 3, labels=["calm", "normal", "volatile"]).astype(str)
    tr["direction"] = np.where(tr["side"] == 1, "spike up (sell)", "dip (buy)")
    seg = {}
    for col in ("session", "eia", "week_open", "vol_regime", "direction"):
        g = tr.groupby(col).agg(trades=("R", "size"), win=("pnl", lambda x: (x > 0).mean()),
                                avgR=("R", "mean"), avgR_gross=("R_gross", "mean"))
        seg[col] = {str(i): {k: float(v) for k, v in row.items()} for i, row in g.iterrows()}
    by_p = tr.groupby(["period", "session"])["R"].mean().unstack().round(3)
    seg["session_by_period"] = {p: {k: float(v) for k, v in row.items()} for p, row in by_p.iterrows()}
    robust["segments_reference"] = seg

    with open(os.path.join(OUT, "summary.json"), "w") as fh:
        json.dump({"meta": meta, "reference": {"detector": REFERENCE.label, **REF_EXIT},
                   "merit": merit, "robust": robust}, fh, indent=1, default=str)
    print(json.dumps({"meta": meta, "merit": merit}, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
