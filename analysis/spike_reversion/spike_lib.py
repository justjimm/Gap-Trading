"""Spike / dip half-retracement research on MT5 M15 exports.

Hypothesis under test: after a rapid spike (or dip) price retraces about half
of the move, more slowly than the move itself, and within roughly a day.

All detection is adaptive: a move only counts as a spike when it is large
relative to the bars *before* it (the "surrounding behaviour"), measured with
a rolling average bar range. Everything here is look-ahead free at decision
time: detection uses bars up to and including the signal bar, entry is the
next bar's open.

Conventions
- MT5 bar prices are Bid. Spread (points, 1 point = 0.001) is charged once per
  round trip at the entry bar's spread.
- If a single bar touches both target and stop, the stop is assumed first.
- Trades never hold over a weekend: the forward path is cut at the last bar of
  the trading week and the trade is closed there at that bar's close.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

POINT = 0.001
HORIZON = 96          # 15-minute bars in a day


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def load_mt5_csv(path: str) -> pd.DataFrame:
    """Load an MT5 'Export bars' CSV (tab separated, <DATE> <TIME> ... header)."""
    df = pd.read_csv(path, sep="\t")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    df["t"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
    df = df.drop(columns=["date", "time"]).sort_values("t").reset_index(drop=True)
    gap = df["t"].diff()
    # Contiguous segment: breaks at any missing bar (daily break, holidays).
    df["seg"] = (gap != pd.Timedelta(minutes=15)).cumsum()
    # Trading week: breaks at gaps longer than a day (weekends, long holidays).
    df["week"] = (gap > pd.Timedelta(days=1)).cumsum()
    # Session follows US daylight saving: first bar 00:00 in US summer, 01:00 in winter.
    first_hour = df.groupby(df["t"].dt.date)["t"].transform("min").dt.hour
    df["us_summer"] = first_hour == 0
    return df


def session_label(t: pd.Series, us_summer: pd.Series) -> pd.Series:
    """Label each bar by market session. Server time is UTC (verified from the
    EIA Wednesday 10:30 ET release and the NY open showing up at 14:30/14:00 UTC
    in winter and 14:30/13:00 UTC in summer)."""
    # Shift summer bars by +1h so both halves of the year share NY-relative hours.
    h = t.dt.hour + t.dt.minute / 60 + np.where(us_summer, 1.0, 0.0)
    lab = np.select(
        [h < 8, h < 13, h < 17, h >= 17],
        ["Asia (pre-London)", "London morning", "London/NY overlap", "NY afternoon"], default="",
    )
    return pd.Series(lab, index=t.index)


def is_eia_bar(t: pd.Series, us_summer: pd.Series) -> pd.Series:
    """Wednesday bar containing the 10:30 ET EIA crude inventory release and the
    following bar (10:30 ET = 15:30 UTC winter, 14:30 UTC summer)."""
    start = np.where(us_summer, 14 * 60 + 30, 15 * 60 + 30)
    m = t.dt.hour * 60 + t.dt.minute
    return (t.dt.dayofweek == 2) & (m >= start) & (m < start + 30)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Detector:
    kind: str      # "atr_move" | "zscore" | "range" | "ema_stretch"
    n: int         # bars in the spike window
    m: int         # bars of prior context used to normalise
    k: float       # threshold in units of the context measure

    @property
    def label(self) -> str:
        return f"{self.kind} N={self.n} M={self.m} k={self.k:g}"


def _rolling_mean_prior(x: np.ndarray, m: int, lag: int) -> np.ndarray:
    """Mean of x over the m bars ending `lag` bars before each bar."""
    s = pd.Series(x).rolling(m, min_periods=m).mean().shift(lag)
    return s.to_numpy()


def detect(df: pd.DataFrame, d: Detector) -> pd.DataFrame:
    """Return one row per spike event (signal at the close of bar i).

    For an up-spike: base P0 = close of the bar before the N-bar window,
    extreme E = highest high inside the window, move = E - P0. Dips mirror it.
    The context measure is computed only from bars *before* the window, so the
    spike cannot inflate its own yardstick.
    """
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    seg = df["seg"].to_numpy()
    n, m = d.n, d.m
    rng = h - l
    ctx_atr = _rolling_mean_prior(rng, m, n)

    hh = pd.Series(h).rolling(n).max().to_numpy()
    ll = pd.Series(l).rolling(n).min().to_numpy()
    base = pd.Series(c).shift(n).to_numpy()
    up_move = hh - base
    dn_move = base - ll

    if d.kind == "atr_move":
        up_score, dn_score = up_move / ctx_atr, dn_move / ctx_atr
    elif d.kind == "zscore":
        # N-bar close-to-close return against the std of N-bar returns in context
        ret = pd.Series(c).diff(n)
        sd = ret.rolling(m, min_periods=m).std().shift(n).to_numpy()
        z = ret.to_numpy() / sd
        up_score, dn_score = z, -z
    elif d.kind == "range":
        # Single-bar range against the median range of the context (N ignored -> 1)
        med = pd.Series(rng).rolling(m, min_periods=m).median().shift(1).to_numpy()
        r = rng / med
        bull = c >= o
        up_score = np.where(bull, r, 0.0)
        dn_score = np.where(~bull, r, 0.0)
        up_move = np.where(bull, h - pd.Series(c).shift(1).to_numpy(), np.nan)
        dn_move = np.where(~bull, pd.Series(c).shift(1).to_numpy() - l, np.nan)
        hh, ll = h, l
        base = pd.Series(c).shift(1).to_numpy()
        n = 1
        ctx_atr = _rolling_mean_prior(rng, m, 1)
    elif d.kind == "ema_stretch":
        # Close stretched away from an EMA of the context, in context-ATR units;
        # the "move" is measured from the EMA to the window extreme.
        ema = pd.Series(c).ewm(span=m, adjust=False).mean().shift(n).to_numpy()
        up_score = (c - ema) / ctx_atr
        dn_score = (ema - c) / ctx_atr
        base = ema
        up_move = hh - ema
        dn_move = ema - ll
    else:
        raise ValueError(d.kind)

    # Window (base bar .. signal bar) must sit inside one contiguous segment.
    contiguous = pd.Series(seg).shift(n).to_numpy() == seg
    valid = contiguous & np.isfinite(ctx_atr) & (ctx_atr > 0)
    up = valid & (up_score >= d.k) & (up_move > 0)
    dn = valid & (dn_score >= d.k) & (dn_move > 0)
    both = up & dn
    up = up & ~(both & (dn_score > up_score))
    dn = dn & ~up

    idx = np.flatnonzero(up | dn)
    # De-duplicate: a spike stays one event until N bars have passed.
    keep, last = [], -10 ** 9
    for i in idx:
        if i - last > n:
            keep.append(i)
            last = i
    idx = np.array(keep, dtype=int)
    idx = idx[idx + 1 < len(df)]

    side = np.where(up[idx], 1, -1)            # +1 = up-spike (we fade by selling)
    ext = np.where(side == 1, hh[idx], ll[idx])
    b = base[idx]
    ev = pd.DataFrame({
        "i": idx,
        "t": df["t"].to_numpy()[idx],
        "side": side,
        "base": b,
        "extreme": ext,
        "move": np.abs(ext - b),
        "atr": ctx_atr[idx],
        "score": np.where(side == 1, up_score[idx], dn_score[idx]),
    })
    ev["move_atr"] = ev["move"] / ev["atr"]
    ev.attrs["m"] = d.m
    return ev


# --------------------------------------------------------------------------
# Forward paths
# --------------------------------------------------------------------------
def forward_paths(df: pd.DataFrame, start: np.ndarray, horizon: int = HORIZON):
    """High/low/close paths for bars start..start+horizon-1, cut at the end of
    the trading week (positions are not held over the weekend). Bars after the
    cut are NaN; `last` is the index offset of the final usable bar."""
    h, l, c = (df[k].to_numpy() for k in ("high", "low", "close"))
    wk = df["week"].to_numpy()
    nbar = len(df)
    off = np.arange(horizon)
    j = start[:, None] + off[None, :]
    inb = j < nbar
    jj = np.where(inb, j, nbar - 1)
    same_week = inb & (wk[jj] == wk[start][:, None])
    H = np.where(same_week, h[jj], np.nan)
    L = np.where(same_week, l[jj], np.nan)
    C = np.where(same_week, c[jj], np.nan)
    last = same_week.sum(axis=1) - 1
    return H, L, C, last


def retracement_stats(df: pd.DataFrame, ev: pd.DataFrame, horizon: int = HORIZON) -> pd.DataFrame:
    """Per event: deepest retracement (fraction of the move) within the horizon,
    bars to reach 25/38.2/50/61.8/100 %, and whether price first pushed beyond
    the extreme (continuation) before retracing 50 %."""
    start = ev["i"].to_numpy() + 1
    H, L, C, last = forward_paths(df, start, horizon)
    side = ev["side"].to_numpy()[:, None]
    ext = ev["extreme"].to_numpy()[:, None]
    mv = ev["move"].to_numpy()[:, None]
    # Retracement fraction reached by each bar (towards the base)
    toward = np.where(side == 1, ext - L, H - ext) / mv
    beyond = np.where(side == 1, H - ext, ext - L) / mv      # >0 means a new extreme
    out = ev.copy()
    with np.errstate(invalid="ignore"):
        out["max_retrace"] = np.nanmax(toward, axis=1)
        for f in (0.25, 0.382, 0.5, 0.618, 1.0):
            hit = toward >= f
            first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
            out[f"bars_to_{f:g}"] = np.where(first >= 0, first + 1, np.nan)
        hit50 = toward >= 0.5
        f50 = np.where(hit50.any(axis=1), hit50.argmax(axis=1), horizon + 1)
        # New extreme by at least 25 % of the move before the 50 % retrace
        cont = beyond >= 0.25
        fc = np.where(cont.any(axis=1), cont.argmax(axis=1), horizon + 1)
    out["continued_first"] = fc < f50
    out["max_extension"] = np.nanmax(np.where(np.isnan(beyond), -np.inf, beyond), axis=1)
    return out


# --------------------------------------------------------------------------
# Trade simulation
# --------------------------------------------------------------------------
def simulate(df: pd.DataFrame, ev: pd.DataFrame, f_target: float, stop_atr: float,
             time_stop: int = HORIZON, entry: str = "next_open",
             cost_mult: float = 1.0, non_overlap: bool = True) -> pd.DataFrame:
    """Fade each event: sell an up-spike / buy a dip at the next bar's open,
    target = extreme -/+ f_target * move (towards the base), stop = extreme
    +/- stop_atr * context ATR, time stop after `time_stop` bars.

    entry="confirm": wait up to 8 bars for the first bar that fails to make a
    new extreme and closes against the spike; enter at the following open.
    If price makes a new extreme while waiting, the extreme (and move) extend.

    Returns one row per trade with pnl in price units and in R
    (R = initial risk = |entry - stop| + spread).
    """
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    spread = df["spread"].to_numpy() * POINT * cost_mult
    wk = df["week"].to_numpy()
    nbar = len(df)

    sig = ev["i"].to_numpy()
    side = ev["side"].to_numpy()
    ext = ev["extreme"].to_numpy().astype(float).copy()
    base = ev["base"].to_numpy()
    atr = ev["atr"].to_numpy()

    if entry == "next_open":
        ent_i = sig + 1
    elif entry == "confirm":
        ent_i = np.full(len(sig), -1)
        for k, i in enumerate(sig):
            for j in range(i + 1, min(i + 9, nbar - 1)):
                if wk[j] != wk[i]:
                    break
                if side[k] == 1:
                    if h[j] > ext[k]:
                        ext[k] = h[j]
                        continue
                    if c[j] < c[j - 1]:
                        ent_i[k] = j + 1
                        break
                else:
                    if l[j] < ext[k]:
                        ext[k] = l[j]
                        continue
                    if c[j] > c[j - 1]:
                        ent_i[k] = j + 1
                        break
    else:
        raise ValueError(entry)

    ok = (ent_i > 0) & (ent_i < nbar)
    ok &= wk[np.clip(ent_i, 0, nbar - 1)] == wk[sig]
    mv = np.abs(ext - base)
    tgt = ext - side * f_target * mv
    stp = ext + side * stop_atr * atr
    ep = np.where(ok, o[np.clip(ent_i, 0, nbar - 1)], np.nan)
    # Skip if the entry is already past the target or beyond the stop.
    ok &= np.where(side == 1, ep > tgt, ep < tgt)
    ok &= np.where(side == 1, ep < stp, ep > stp)

    sel = np.flatnonzero(ok)
    if len(sel) == 0:
        return pd.DataFrame()
    H, L, C, last = forward_paths(df, ent_i[sel], time_stop)
    s = side[sel][:, None]
    t_ = tgt[sel][:, None]
    st = stp[sel][:, None]
    # Short (fading up-spike): target when low <= tgt, stop when high >= stp.
    hit_t = np.where(s == 1, L <= t_, H >= t_)
    hit_s = np.where(s == 1, H >= st, L <= st)
    BIG = time_stop + 10
    ft = np.where(hit_t.any(1), hit_t.argmax(1), BIG)
    fs = np.where(hit_s.any(1), hit_s.argmax(1), BIG)
    stop_first = fs <= ft                       # same bar -> stop (conservative)
    exit_off = np.where(stop_first, fs, ft)
    timed = exit_off >= BIG
    exit_off = np.where(timed, last, exit_off)
    exit_px = np.where(timed, C[np.arange(len(sel)), last],
                       np.where(stop_first, stp[sel], tgt[sel]))
    sp = spread[ent_i[sel]]
    pnl = -side[sel] * (exit_px - ep[sel]) - sp
    risk = np.abs(ep[sel] - stp[sel]) + sp
    out = pd.DataFrame({
        "sig_i": sig[sel], "entry_i": ent_i[sel], "exit_i": ent_i[sel] + exit_off,
        "t": df["t"].to_numpy()[ent_i[sel]], "side": side[sel],
        "entry": ep[sel], "target": tgt[sel], "stop": stp[sel], "exit": exit_px,
        "outcome": np.where(timed, "time", np.where(stop_first, "stop", "target")),
        "bars_held": exit_off + 1, "spread": sp, "pnl": pnl, "risk": risk,
    })
    out["R"] = out["pnl"] / out["risk"]
    if non_overlap:
        keep, busy_until = [], -1
        for k, (ei, xi) in enumerate(zip(out["entry_i"].to_numpy(), out["exit_i"].to_numpy())):
            if ei > busy_until:
                keep.append(k)
                busy_until = xi
        out = out.iloc[keep].reset_index(drop=True)
    return out


def summarise(tr: pd.DataFrame) -> dict:
    if tr is None or len(tr) == 0:
        return {"trades": 0, "win": np.nan, "avgR": np.nan, "pf": np.nan, "totalR": 0.0,
                "pnl_pts": 0.0, "pct_target": np.nan, "pct_stop": np.nan, "pct_time": np.nan}
    w = tr["pnl"] > 0
    gp, gl = tr.loc[w, "pnl"].sum(), -tr.loc[~w, "pnl"].sum()
    return {
        "trades": int(len(tr)),
        "win": float(w.mean()),
        "avgR": float(tr["R"].mean()),
        "pf": float(gp / gl) if gl > 0 else np.inf,
        "totalR": float(tr["R"].sum()),
        "pnl_pts": float(tr["pnl"].sum()),
        "pct_target": float((tr["outcome"] == "target").mean()),
        "pct_stop": float((tr["outcome"] == "stop").mean()),
        "pct_time": float((tr["outcome"] == "time").mean()),
    }


def random_baseline(df: pd.DataFrame, tr: pd.DataFrame, ev: pd.DataFrame, lo: int, hi: int,
                    reps: int = 30, seed: int = 7, time_stop: int = HORIZON) -> dict:
    """Same trade geometry, random time and random direction.

    For every real trade, draw a random bar in [lo, hi), keep the trade's target
    and stop distances in units of the context ATR, rescale to the random bar's
    ATR, pick a random direction and simulate. Repeated `reps` times. This asks:
    is the win rate / expectancy any better than the same bracket placed blindly?
    """
    if len(tr) == 0:
        return {}
    rng_ = np.random.default_rng(seed)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    spread = df["spread"].to_numpy() * POINT
    ctx = _rolling_mean_prior(h - l, int(ev.attrs.get("m", 32)), 1)
    atr_by_sig = dict(zip(ev["i"], ev["atr"]))
    a_ev = np.array([atr_by_sig[i] for i in tr["sig_i"]])
    td = np.abs(tr["entry"] - tr["target"]).to_numpy() / a_ev
    sd = np.abs(tr["entry"] - tr["stop"]).to_numpy() / a_ev
    res = []
    for _ in range(reps):
        cand = rng_.integers(lo, hi - 1, size=len(tr) * 3)
        cand = cand[np.isfinite(ctx[cand])][: len(tr)]
        k = len(cand)
        a = ctx[cand]
        d = rng_.choice([-1, 1], size=k)          # -1 = buy, +1 = sell (fade convention)
        ep = o[cand]
        tgt = ep - d * td[:k] * a
        stp = ep + d * sd[:k] * a
        H, L, C, last = forward_paths(df, cand, time_stop)
        s = d[:, None]
        hit_t = np.where(s == 1, L <= tgt[:, None], H >= tgt[:, None])
        hit_s = np.where(s == 1, H >= stp[:, None], L <= stp[:, None])
        BIG = time_stop + 10
        ft = np.where(hit_t.any(1), hit_t.argmax(1), BIG)
        fs = np.where(hit_s.any(1), hit_s.argmax(1), BIG)
        stop_first = fs <= ft
        timed = np.minimum(ft, fs) >= BIG
        xp = np.where(timed, C[np.arange(k), last], np.where(stop_first, stp, tgt))
        sp = spread[cand]
        pnl = -d * (xp - ep) - sp
        R = pnl / (np.abs(ep - stp) + sp)
        res.append(((pnl > 0).mean(), R.mean()))
    res = np.array(res)
    return {"win": float(res[:, 0].mean()), "avgR": float(res[:, 1].mean()),
            "avgR_p05": float(np.percentile(res[:, 1], 5)),
            "avgR_p95": float(np.percentile(res[:, 1], 95))}
