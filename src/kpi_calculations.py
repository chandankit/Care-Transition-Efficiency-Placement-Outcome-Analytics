"""KPI formulas, period aggregation and rolling metrics.

Conventions used everywhere:
* Ratios are percentages (x100) and are always computed from components,
  never by averaging daily ratios: a period ratio is a *pooled* ratio,
  sum(numerator) / sum(denominator) over records where both are present.
* Flow totals cover reported days only. Because ~1/3 of calendar days are
  unreported, periods are compared on *mean per reported day*, not totals.
* Stocks are snapshots: they are never summed across days. A period's stock
  is described by its opening, closing, mean, min and max snapshots.
* Zero or missing denominators give NaN, never 0 or infinity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.pipeline_analysis import implied_hhs_admissions

FREQ_COLUMNS = {"W": "week", "M": "month", "Q": "quarter"}


def safe_divide(numerator, denominator):
    """Division returning NaN where the denominator is 0, negative or missing.

    Accepts scalars or Series (any combination); counts are never negative,
    so a non-positive denominator signals an undefined ratio.
    """
    if np.isscalar(numerator) and np.isscalar(denominator):
        ok = pd.notna(numerator) and pd.notna(denominator) and denominator > 0
        return float(numerator) / float(denominator) if ok else np.nan
    num = numerator.astype(float) if isinstance(numerator, pd.Series) else float(numerator)
    den = denominator.astype(float) if isinstance(denominator, pd.Series) else float(denominator)
    result = num / den
    valid = den > 0
    if np.isscalar(valid):
        return result if valid else result * np.nan
    return result.where(valid)


def pooled_ratio(numerator: pd.Series, denominator: pd.Series, scale: float = 100.0) -> float:
    """sum(numerator)/sum(denominator) over rows where both are present."""
    both = numerator.notna() & denominator.notna()
    if not both.any():
        return np.nan
    return safe_divide(numerator[both].sum(), denominator[both].sum()) * scale


# --------------------------------------------------------------------------
# Record-level metrics
# --------------------------------------------------------------------------
def add_daily_metrics(df: pd.DataFrame, denominator: str = "same_day") -> pd.DataFrame:
    """Add record-level KPIs.

    denominator:
      "same_day"          - flow(t) / stock(t), the same day's reported snapshot;
      "previous_snapshot" - flow(t) / stock(t-1), only when the previous report is
                            exactly one day earlier (approximates the opening stock).
    """
    if denominator not in {"same_day", "previous_snapshot"}:
        raise ValueError("denominator must be 'same_day' or 'previous_snapshot'")
    out = df.copy()
    # Recomputed from the rows actually passed in, so subsets stay consistent.
    gap = out["date"].diff().dt.days
    out["gap_days"] = gap
    if denominator == "same_day":
        cbp_den, hhs_den = out["cbp_custody"], out["hhs_care"]
    else:
        cbp_den = out["cbp_custody"].shift(1).where(gap.eq(1))
        hhs_den = out["hhs_care"].shift(1).where(gap.eq(1))
    # Kept as columns so pooled period and rolling ratios use the same denominator.
    out["cbp_ratio_denominator"] = cbp_den
    out["hhs_ratio_denominator"] = hhs_den
    out["transfer_to_stock_pct"] = safe_divide(out["cbp_transfers_out"], cbp_den) * 100
    out["discharge_to_stock_pct"] = safe_divide(out["hhs_discharges"], hhs_den) * 100

    # Changes between consecutive reported snapshots (whatever the gap length),
    # plus a per-calendar-day version that is comparable across gaps.
    out["cbp_change"] = out["cbp_custody"].diff()
    out["hhs_change"] = out["hhs_care"].diff()
    out["cbp_change_per_day"] = safe_divide(out["cbp_change"], gap)
    out["hhs_change_per_day"] = safe_divide(out["hhs_change"], gap)
    out["combined_population"] = out["cbp_custody"] + out["hhs_care"]
    out["combined_change"] = out["combined_population"].diff()

    out["cbp_net_flow"] = out["cbp_intake"] - out["cbp_transfers_out"]
    out["hhs_net_flow"] = out["cbp_transfers_out"] - out["hhs_discharges"]
    out["system_net_flow"] = out["cbp_intake"] - out["hhs_discharges"]
    out["implied_hhs_admissions"] = implied_hhs_admissions(out)
    return out


# --------------------------------------------------------------------------
# Period KPIs
# --------------------------------------------------------------------------
def _stock_stats(s: pd.Series, prefix: str) -> dict[str, float]:
    v = s.dropna()
    if v.empty:
        return {f"{prefix}_{k}": np.nan for k in ("opening", "closing", "mean", "min", "max", "change")}
    return {f"{prefix}_opening": v.iloc[0], f"{prefix}_closing": v.iloc[-1],
            f"{prefix}_mean": v.mean(), f"{prefix}_min": v.min(), f"{prefix}_max": v.max(),
            f"{prefix}_change": v.iloc[-1] - v.iloc[0]}


def ratio_denominator(df: pd.DataFrame, stage: str) -> str:
    """Denominator column for TSR ('cbp') or DSR ('hhs'): the one chosen in
    add_daily_metrics if present, else the same-day stock."""
    col = f"{stage}_ratio_denominator"
    return col if col in df else ("cbp_custody" if stage == "cbp" else "hhs_care")


def period_kpis(df: pd.DataFrame) -> dict[str, float]:
    """All KPIs for one period (any set of records)."""
    if df.empty:
        return {"records": 0}
    first, last = df["date"].min(), df["date"].max()
    days = (last - first).days + 1
    k: dict[str, float] = {"records": len(df), "first_date": first, "last_date": last,
                           "calendar_days": days, "reported_share": len(df) / days}
    for col in ("cbp_intake", "cbp_transfers_out", "hhs_discharges"):
        k[f"{col}_total"] = df[col].sum(min_count=1)
        k[f"{col}_per_day"] = df[col].mean()
        k[f"{col}_n"] = int(df[col].notna().sum())
    k.update(_stock_stats(df["cbp_custody"], "cbp_custody"))
    k.update(_stock_stats(df["hhs_care"], "hhs_care"))

    k["transfer_to_stock_pct"] = pooled_ratio(df["cbp_transfers_out"], df[ratio_denominator(df, "cbp")])
    k["discharge_to_stock_pct"] = pooled_ratio(df["hhs_discharges"], df[ratio_denominator(df, "hhs")])
    k["throughput_cbp_stage_pct"] = pooled_ratio(df["cbp_transfers_out"], df["cbp_intake"])
    k["throughput_hhs_stage_pct"] = pooled_ratio(df["hhs_discharges"], df["cbp_transfers_out"])
    k["throughput_system_pct"] = pooled_ratio(df["hhs_discharges"], df["cbp_intake"])

    d = df["hhs_discharges"].dropna()
    k["discharge_std"] = d.std(ddof=1) if len(d) > 1 else np.nan
    k["discharge_cv"] = safe_divide(k["discharge_std"], d.mean()) if len(d) > 1 else np.nan
    both = df["hhs_care"].notna() & df["hhs_discharges"].notna()
    # Inverse of the pooled DSR expressed in days. Equals average time in care
    # only in a steady state (Little's law); see KPI dictionary.
    k["stock_to_discharge_days"] = safe_divide(df.loc[both, "hhs_care"].mean(),
                                               df.loc[both, "hhs_discharges"].mean())
    return k


def aggregate(df: pd.DataFrame, freq: str, history: pd.DataFrame | None = None) -> pd.DataFrame:
    """Aggregate records to weekly ('W'), monthly ('M') or quarterly ('Q') KPIs.

    Weeks end on Saturday (reports run Sunday-Thursday). Stock change compares
    each period's closing snapshot with the previous period's closing snapshot;
    period-over-period % change compares mean flows per reported day. When
    ``history`` (e.g. the unfiltered records) is given, the first period is
    compared with the preceding period in ``history`` instead of being NaN.
    """
    if freq == "D":
        return df.copy()
    if freq not in FREQ_COLUMNS:
        raise ValueError(f"freq must be one of D, {', '.join(FREQ_COLUMNS)}")
    key = FREQ_COLUMNS[freq]
    periods = df[key] if key in df else df["date"].dt.to_period("W-SAT" if freq == "W" else freq)
    rows = []
    for period, part in df.groupby(periods, sort=True, observed=True):
        row = period_kpis(part)
        row["period"] = period
        rows.append(row)
    if not rows:
        return pd.DataFrame(index=pd.Index([], name="period"))
    out = pd.DataFrame(rows).set_index("period")
    flows = ("cbp_intake", "cbp_transfers_out", "hhs_discharges")
    prev_close = {s: out[f"{s}_closing"].shift(1) for s in ("cbp_custody", "hhs_care")}
    prev_flow = {c: out[f"{c}_per_day"].shift(1) for c in flows}
    if history is not None and len(history):
        hist = aggregate(history[history["date"] < df["date"].min()], freq)
        if len(hist) and hist.index[-1] == out.index[0] - 1:
            for s in prev_close:
                prev_close[s].iloc[0] = hist[f"{s}_closing"].iloc[-1]
            for c in flows:
                prev_flow[c].iloc[0] = hist[f"{c}_per_day"].iloc[-1]
    for s, prev in prev_close.items():
        out[f"{s}_period_change"] = out[f"{s}_closing"] - prev
    for c, prev in prev_flow.items():
        out[f"{c}_per_day_pct_change"] = (safe_divide(out[f"{c}_per_day"], prev) - 1) * 100
    out["period_start"] = out.index.to_timestamp(how="start")
    return out


# --------------------------------------------------------------------------
# Rolling metrics (calendar windows)
# --------------------------------------------------------------------------
def _rolling_pooled(frame: pd.DataFrame, num: str, den: str, window: str,
                    min_records: int) -> pd.Series:
    both = frame[num].notna() & frame[den].notna()
    n = frame[num].where(both).rolling(window)
    d = frame[den].where(both).rolling(window)
    count = frame[num].where(both).rolling(window).count()
    ratio = safe_divide(n.sum(), d.sum()) * 100
    return ratio.where(count >= min_records)


def rolling_metrics(df: pd.DataFrame, window_days: int, min_records: int = 5) -> pd.DataFrame:
    """Trailing calendar-window metrics for every record.

    Means are per reported day inside the window; ratios are pooled. Values
    need at least ``min_records`` reported days in the window.
    """
    w = f"{int(window_days)}D"
    frame = df.set_index("date")
    out = pd.DataFrame(index=frame.index)
    for col in ("cbp_intake", "cbp_transfers_out", "hhs_discharges", "cbp_custody", "hhs_care"):
        r = frame[col].rolling(w)
        cnt = r.count()
        out[f"{col}_mean"] = r.mean().where(cnt >= min_records)
        out[f"{col}_std"] = r.std().where(cnt >= min_records)
    out["hhs_discharges_cv"] = safe_divide(out["hhs_discharges_std"], out["hhs_discharges_mean"])
    out["transfer_to_stock_pct"] = _rolling_pooled(frame, "cbp_transfers_out", ratio_denominator(frame, "cbp"),
                                                   w, min_records)
    out["discharge_to_stock_pct"] = _rolling_pooled(frame, "hhs_discharges", ratio_denominator(frame, "hhs"),
                                                    w, min_records)
    out["hhs_entry_exit_ratio"] = _rolling_pooled(frame, "cbp_transfers_out", "hhs_discharges", w, min_records) / 100
    out["cbp_entry_exit_ratio"] = _rolling_pooled(frame, "cbp_intake", "cbp_transfers_out", w, min_records) / 100
    for col in ("cbp_net_flow", "hhs_net_flow"):
        if col in frame:
            r = frame[col].rolling(w)
            out[f"{col}_mean"] = r.mean().where(r.count() >= min_records)
    # Stock change per calendar day, pooled: sum(change) / sum(days covered).
    # Averaging per-report rates would weight a 3-day gap like a single day.
    gap = pd.Series(frame.index.to_series().diff().dt.days.to_numpy(), index=frame.index)
    for stage, stock in (("cbp", "cbp_custody"), ("hhs", "hhs_care")):
        change = frame[stock].diff()
        both = change.notna() & gap.notna()
        num = change.where(both).rolling(w)
        den = gap.where(both).rolling(w)
        out[f"{stage}_change_per_day_mean"] = safe_divide(num.sum(), den.sum()).where(num.count() >= min_records)
    out.index = df.index
    out.insert(0, "date", df["date"].to_numpy())
    return out


# --------------------------------------------------------------------------
# KPI dictionary (single source of truth for app + reports)
# --------------------------------------------------------------------------
KPI_DEFINITIONS: list[dict[str, str]] = [
    {
        "kpi": "Transfer-to-Stock Ratio (TSR)",
        "formula": "100 x transfers out of CBP(t) / children in CBP custody(t)",
        "columns": "Children transferred out of CBP custody; Children in CBP custody",
        "unit": "% of reported CBP custody per reported day",
        "frequency": "Daily; period value is pooled: 100 x sum(transfers) / sum(custody snapshots)",
        "denominator": "Same-day reported snapshot (default). Option: previous snapshot, only when it is "
                       "exactly one day earlier. Whether the snapshot is opening or closing is undocumented.",
        "missing": "NaN when custody is 0 or missing, or transfers missing. Never 0 or infinity.",
        "interpretation": "Descriptive: transfer activity relative to the reported custody population. "
                          "Higher values mean more transfers per child reported in custody.",
        "limitations": "Not a transfer probability and not a processing time. Values above 100% are "
                       "possible because custody is a point-in-time snapshot while transfers accumulate "
                       "over a day. Unavailable where CBP values are screened out.",
    },
    {
        "kpi": "Discharge-to-Stock Ratio (DSR)",
        "formula": "100 x discharges from HHS care(t) / children in HHS care(t)",
        "columns": "Children discharged from HHS Care; Children in HHS Care",
        "unit": "% of reported HHS care population per reported day",
        "frequency": "Daily; period value pooled as for TSR",
        "denominator": "Same-day reported snapshot (default) or one-day-earlier snapshot.",
        "missing": "NaN when HHS care is 0 or missing, or discharges missing.",
        "interpretation": "Descriptive: discharge activity relative to the reported HHS population.",
        "limitations": "Not a placement success rate. HHS describes discharges as children 'united to a "
                       "vetted sponsor' but publishes no categories or counting rules, and the data do not "
                       "track cohorts. The children discharged are not necessarily those in the denominator.",
    },
    {
        "kpi": "Stock-to-Discharge Ratio (days)",
        "formula": "mean HHS care / mean daily discharges over the period (= 100 / pooled DSR)",
        "columns": "Children in HHS Care; Children discharged from HHS Care",
        "unit": "days",
        "frequency": "Weekly, monthly, quarterly, or selected period",
        "denominator": "Mean discharges per reported day",
        "missing": "NaN when there are no discharges in the period.",
        "interpretation": "How many days of discharges at the current pace the reported population "
                          "represents. Under a steady state (Little's law) it approximates mean time in care.",
        "limitations": "Not a measured length of care. Biased whenever the population is growing or "
                       "shrinking, which is most of this dataset.",
    },
    {
        "kpi": "Pipeline Throughput",
        "formula": "CBP stage: sum(transfers)/sum(intake). HHS stage: sum(discharges)/sum(transfers). "
                   "System: sum(HHS discharges)/sum(CBP intake). All x100.",
        "columns": "Intake, transfers out of CBP, HHS discharges",
        "unit": "%",
        "frequency": "Weekly, monthly, quarterly, or selected period",
        "denominator": "Entries to the stated boundary over the same reported days as the exits",
        "missing": "Only records where both flows are present are used.",
        "interpretation": "Above 100%: more exits than entries on reported days (stock drawn down). "
                          "Below 100%: entries exceed exits (stock builds). The system ratio counts the "
                          "internal CBP->HHS transfer only once.",
        "limitations": "System ratio ignores CBP exits other than transfers (none are reported). HHS "
                       "stage assumes transfers out of CBP are the HHS admissions. Unreported days are "
                       "excluded from both numerator and denominator.",
    },
    {
        "kpi": "CBP / HHS Population Change",
        "formula": "stock(t) - stock(previous report); per-day version divides by the gap in days. "
                   "Period change: closing snapshot - previous period's closing snapshot.",
        "columns": "Children in CBP custody; Children in HHS Care",
        "unit": "children (or children per calendar day)",
        "frequency": "Between consecutive reports; weekly/monthly/quarterly",
        "denominator": "Gap in calendar days (per-day version)",
        "missing": "NaN if either snapshot is missing or screened out.",
        "interpretation": "Growth or reduction in the reported population.",
        "limitations": "Snapshots only; the change mixes entries and exits. The combined CBP+HHS "
                       "population is not a count of unique children.",
    },
    {
        "kpi": "Net Flow Imbalance",
        "formula": "CBP: intake - transfers. HHS: transfers - discharges. Window ratio: "
                   "sum(entries)/sum(exits) over a trailing window.",
        "columns": "Intake, transfers, discharges",
        "unit": "children per reported day; ratio",
        "frequency": "Daily; trailing window (default 28 days)",
        "denominator": "Exits over the same records (ratio form)",
        "missing": "Records with any missing component are excluded from the window sums.",
        "interpretation": "Positive / ratio above 1: entries exceed exits on reported days.",
        "limitations": "Flows on unreported days are unknown. CBP stock-flow does not reconcile even "
                       "on consecutive days, so CBP imbalance is indicative only.",
    },
    {
        "kpi": "Backlog Pressure Indicator",
        "formula": "Per stage: sustained stock growth rule AND (configurable: OR) entries-exceed-exits "
                   "rule, both evaluated on the trailing window.",
        "columns": "Stage stock and flows",
        "unit": "flag (true/false) per record; episodes of 5+ consecutive flagged records",
        "frequency": "Daily evaluation over a trailing window",
        "denominator": "-",
        "missing": "Not evaluated (NA) when the window has too few reported days.",
        "interpretation": "Flags potential operational pressure for review.",
        "limitations": "Not a verified queue length or waiting time; does not identify causes.",
    },
    {
        "kpi": "Discharge Stability Indicator",
        "formula": "Coefficient of variation = std / mean of daily discharges, over a trailing window "
                   "or a calendar period; plus month-over-month % change of mean daily discharges.",
        "columns": "Children discharged from HHS Care",
        "unit": "ratio (unitless); %",
        "frequency": "Rolling window; monthly",
        "denominator": "Mean daily discharges (NaN when 0)",
        "missing": "Needs at least the configured number of reported days.",
        "interpretation": "Lower CV = more consistent discharge activity.",
        "limitations": "Day-of-week reporting patterns inflate daily variability; CV is unstable when "
                       "mean discharges are very small (e.g. late 2025).",
    },
    {
        "kpi": "Bottleneck Alert",
        "formula": "Any configured rule (see Bottleneck Detection) firing for 5+ consecutive records; "
                   "alert tier = number of rules active at once (equal weights).",
        "columns": "All",
        "unit": "episode (start, end, duration, magnitude)",
        "frequency": "Daily evaluation",
        "denominator": "-",
        "missing": "Rules are not evaluated on insufficient data.",
        "interpretation": "Periods that merit operational review.",
        "limitations": "Rule-based screening, not proof of a delay; causes require other evidence.",
    },
    {
        "kpi": "Implied HHS Admissions (derived)",
        "formula": "HHS care(t) - HHS care(t-1) + discharges(t), one-day gaps only",
        "columns": "Children in HHS Care; Children discharged from HHS Care",
        "unit": "children per day",
        "frequency": "Daily (consecutive days only)",
        "denominator": "-",
        "missing": "NaN across reporting gaps or missing values.",
        "interpretation": "HHS-side estimate of admissions that does not rely on the CBP columns.",
        "limitations": "Derived, not reported. Equals admissions only if the stock-flow identity holds "
                       "with no other adjustments.",
    },
]
