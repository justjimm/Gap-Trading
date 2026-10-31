"""Collect the numbers the HTML report needs into results/report_data.json."""
import json, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spike_lib import load_mt5_csv, retracement_stats
from run_research import DEFAULT_CSV, OUT, REFERENCE, OOS_START, detect

df = load_mt5_csv(DEFAULT_CSV)
cut = int(df.index[df["t"] >= OOS_START][0])
S = json.load(open(os.path.join(OUT, "summary.json")))
ev = detect(df, REFERENCE)
e = ev[ev["i"] >= cut]
rs = retracement_stats(df, e)

# Null: random bars, same move sizes (in context-ATR units), same geometry
rng = np.random.default_rng(1)
cand = rng.integers(cut, len(df) - 2, size=len(e) * 4)
c = df["close"].to_numpy()
hh = df["high"].rolling(REFERENCE.n).max().to_numpy()
ll = df["low"].rolling(REFERENCE.n).min().to_numpy()
base = df["close"].shift(REFERENCE.n).to_numpy()
ctx = (df["high"] - df["low"]).rolling(REFERENCE.m).mean().shift(REFERENCE.n).to_numpy()
up = c[cand] >= base[cand]
fake = pd.DataFrame({"i": cand, "side": np.where(up, 1, -1), "extreme": np.where(up, hh[cand], ll[cand])})
fake["move"] = rng.choice(e["move_atr"].to_numpy(), size=len(fake)) * ctx[cand]
fake["base"] = fake["extreme"] - fake["side"] * fake["move"]
fake = fake[np.isfinite(fake["move"]) & (fake["move"] > 0)]
rn = retracement_stats(df, fake)

H = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96]
curve = {"h": H,
         "spike": [float((rs["bars_to_0.5"] <= h).mean()) for h in H],
         "random": [float((rn["bars_to_0.5"] <= h).mean()) for h in H]}
depth_bins = [0, .25, .382, .5, .618, 1.0, 1.5, np.inf]
lab = ["<25%", "25–38%", "38–50%", "50–62%", "62–100%", "100–150%", ">150%"]
dh = pd.cut(rs["max_retrace"], depth_bins, right=False, labels=lab).value_counts(sort=False)
dn = pd.cut(rn["max_retrace"], depth_bins, right=False, labels=lab).value_counts(sort=False)
depth = {"labels": lab, "spike": (dh / dh.sum()).round(4).tolist(), "random": (dn / dn.sum()).round(4).tolist()}

sw = pd.read_csv(os.path.join(OUT, "sweep.csv"))
sw = sw[(sw.IS_trades >= 40) & (sw.OOS_trades >= 40)]
samp = sw.sample(min(1800, len(sw)), random_state=3)
pocket = (sw.kind == "zscore") & (sw.N == 8) & (sw.M.isin([192, 384])) & (sw.k.isin([2.5, 3])) & \
         (sw.entry == "confirm") & (sw.stop_atr <= 1) & (sw.f_target == 0.5) & (sw.time_stop == 48)
scatter = {"all": samp[["IS_avgR", "OOS_avgR"]].round(4).values.tolist(),
           "pocket": sw.loc[pocket, ["IS_avgR", "OOS_avgR", "M", "k", "stop_atr"]].round(4).values.tolist()}
json.dump({"curve": curve, "depth": depth, "scatter": scatter,
           "summary": S, "pocket": json.load(open(os.path.join(OUT, "candidate_pocket.json"))),
           "null_n": int(len(rn)), "event_n": int(len(rs))},
          open(os.path.join(OUT, "report_data.json"), "w"), default=float)
print(curve, depth, len(scatter["pocket"]))
