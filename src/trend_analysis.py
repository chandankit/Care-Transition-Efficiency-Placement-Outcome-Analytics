"""Temporal patterns, trends, level shifts and outcome stability.

All results are descriptive associations. Statistical tests are reported with
their assumptions; serial correlation in daily/monthly operational series
means p-values should be read as indicative, not confirmatory.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from config import SIGNIFICANCE_LEVEL
from src.kpi_calculations import aggregate, period_kpis

MIDWEEK = ["Monday", "Tuesday", "Wednesday", "Thursday"]


def weekday_profile(df: pd.DataFrame, metrics: list[str]) -> pd.DataFrame:
    """Records, mean and median of each metric by weekday (Sunday first)."""
    g = df.groupby("weekday", observed=False)
    parts = {"records": g["date"].count()}
    for m in metrics:
        parts[f"{m}_mean"] = g[m].mean()
        parts[f"{m}_median"] = g[m].median()
        parts[f"{m}_n"] = g[m].count()
    return pd.DataFrame(parts)


def weekday_relative_index(df: pd.DataFrame, metric: str, min_midweek: int = 2) -> dict:
    """Each day's value relative to the same reporting week's Mon-Thu mean.

    Comparing within a week removes the large level shifts between periods.
    Sunday is tested against the midweek mean with a Wilcoxon signed-rank test
    on (index - 1). Fridays/Saturdays are essentially never reported.
    """
    frame = df[["date", "week", "weekday", metric]].dropna(subset=[metric]).copy()
    frame["weekday"] = frame["weekday"].astype(str)
    mid = frame[frame["weekday"].isin(MIDWEEK)].groupby("week")[metric].agg(["mean", "count"])
    mid = mid[(mid["count"] >= min_midweek) & (mid["mean"] > 0)]
    frame = frame.join(mid["mean"].rename("midweek_mean"), on="week").dropna(subset=["midweek_mean"])
    frame["index"] = frame[metric] / frame["midweek_mean"]
    order = ["Sunday", *MIDWEEK, "Friday", "Saturday"]
    summary = (frame.groupby("weekday")["index"].agg(["count", "median", "mean"])
               .reindex(order).rename(columns={"count": "weeks"}))
    sunday = frame.loc[frame["weekday"].eq("Sunday"), "index"] - 1
    result = {"metric": metric, "summary": summary, "n_sundays": int(sunday.size),
              "sunday_median_index": float(sunday.median() + 1) if sunday.size else np.nan,
              "p_value": np.nan, "statistic": np.nan}
    if sunday.size >= 10 and (sunday != 0).any():
        test = stats.wilcoxon(sunday, zero_method="wilcox", alternative="two-sided")
        result["statistic"], result["p_value"] = float(test.statistic), float(test.pvalue)
    result["significant"] = bool(result["p_value"] < SIGNIFICANCE_LEVEL) if pd.notna(result["p_value"]) else False
    return result


def trend_test(values: pd.Series, min_n: int = 6) -> dict:
    """Kendall's tau against time order plus Theil-Sen slope (per period).

    Equivalent to a Mann-Kendall test without autocorrelation correction.
    """
    v = values.dropna()
    out = {"n": int(v.size), "tau": np.nan, "p_value": np.nan, "slope": np.nan,
           "slope_low": np.nan, "slope_high": np.nan, "direction": "insufficient data"}
    if v.size < min_n:
        return out
    t = np.arange(v.size, dtype=float)
    tau = stats.kendalltau(t, v.to_numpy(dtype=float))
    ts = stats.theilslopes(v.to_numpy(dtype=float), t, alpha=0.95)
    out.update(tau=float(tau.statistic), p_value=float(tau.pvalue), slope=float(ts.slope),
               slope_low=float(ts.low_slope), slope_high=float(ts.high_slope))
    if tau.pvalue < SIGNIFICANCE_LEVEL:
        out["direction"] = "increasing" if tau.statistic > 0 else "decreasing"
    else:
        out["direction"] = "no significant monotonic trend"
    return out


def detect_level_shift(values: pd.Series, min_size: int = 3) -> dict:
    """Best single change point in the mean (least-squares split).

    Returns the first period of the new level, the means before/after and the
    share of variance explained by the split.
    """
    v = values.dropna()
    n = v.size
    if n < 2 * min_size:
        return {"index": None, "explained_share": np.nan}
    x = v.to_numpy(dtype=float)
    total = ((x - x.mean()) ** 2).sum()
    best_k, best_sse = None, np.inf
    for k in range(min_size, n - min_size + 1):
        a, b = x[:k], x[k:]
        sse = ((a - a.mean()) ** 2).sum() + ((b - b.mean()) ** 2).sum()
        if sse < best_sse:
            best_k, best_sse = k, sse
    before, after = x[:best_k].mean(), x[best_k:].mean()
    return {"index": v.index[best_k], "mean_before": before, "mean_after": after,
            "pct_change": (after / before - 1) * 100 if before else np.nan,
            "explained_share": 1 - best_sse / total if total > 0 else np.nan}


def monthly_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Monthly KPIs with month-over-month change of mean daily flows."""
    m = aggregate(df, "M")
    cols = ["records", "cbp_intake_per_day", "cbp_transfers_out_per_day", "hhs_discharges_per_day",
            "cbp_custody_closing", "hhs_care_closing", "hhs_care_period_change",
            "transfer_to_stock_pct", "discharge_to_stock_pct", "throughput_hhs_stage_pct",
            "throughput_system_pct", "discharge_cv", "stock_to_discharge_days",
            "hhs_discharges_per_day_pct_change", "cbp_transfers_out_per_day_pct_change"]
    return m[[c for c in cols if c in m]]


def stability_summary(df: pd.DataFrame, freq: str = "Q") -> pd.DataFrame:
    """Mean, standard deviation and CV of daily discharges per period."""
    a = aggregate(df, freq)
    return a[["records", "hhs_discharges_n", "hhs_discharges_per_day", "discharge_std", "discharge_cv",
              "discharge_to_stock_pct"]]


def compare_periods(df: pd.DataFrame, a: tuple, b: tuple,
                    metrics: tuple[str, ...] = ("hhs_discharges", "discharge_to_stock_pct",
                                                "cbp_transfers_out", "transfer_to_stock_pct")) -> dict:
    """Period KPIs for two date ranges plus Mann-Whitney tests on daily values."""
    def subset(rng):
        lo, hi = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
        return df[(df["date"] >= lo) & (df["date"] <= hi)]

    pa, pb = subset(a), subset(b)
    kpis = pd.DataFrame({"Period A": pd.Series(period_kpis(pa)), "Period B": pd.Series(period_kpis(pb))})
    tests = []
    for m in metrics:
        if m not in df:
            continue
        xa, xb = pa[m].dropna(), pb[m].dropna()
        row = {"metric": m, "median_A": xa.median(), "median_B": xb.median(), "n_A": xa.size,
               "n_B": xb.size, "p_value": np.nan}
        if xa.size >= 5 and xb.size >= 5:
            row["p_value"] = float(stats.mannwhitneyu(xa, xb, alternative="two-sided").pvalue)
        tests.append(row)
    return {"kpis": kpis, "tests": pd.DataFrame(tests)}
