"""Stage model of the care pipeline and stock-flow reconciliation.

Pipeline:  CBP intake -> CBP custody (stock) -> transfers out of CBP
           -> HHS care (stock) -> HHS discharges.

Reconciliation convention (one reporting day, t-1 -> t):
    CBP expected(t) = CBP custody(t-1) + intake(t) - transfers out(t)
    HHS expected(t) = HHS care(t-1)    + transfers(t) - discharges(t)
    residual(t)     = reported(t) - expected(t)

Assumptions (not documented in the source and therefore flagged):
  * transfers out of CBP on day t enter HHS on day t;
  * CBP transfers are the only HHS admissions;
  * stocks are end-of-day snapshots aligned with the flow window.
Residuals absorb timing differences, other entry/exit routes and reporting
adjustments. They are only *comparable* when consecutive records are one
calendar day apart; across gaps they also contain unreported days' flows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ensure_gap_days(df: pd.DataFrame) -> pd.Series:
    """Days since the previous row *of this frame* (not of the full records), so
    that subsets with rows removed never treat a multi-day pair as one day."""
    return df["date"].diff().dt.days


def reconcile_stock_flow(df: pd.DataFrame) -> pd.DataFrame:
    """Record-level expected vs reported stocks for both stages."""
    gap = ensure_gap_days(df)
    out = pd.DataFrame({"date": df["date"], "gap_days": gap})
    out["cbp_expected"] = (df["cbp_custody"].shift(1) + df["cbp_intake"]
                           - df["cbp_transfers_out"])
    out["cbp_residual"] = df["cbp_custody"] - out["cbp_expected"]
    out["hhs_expected"] = (df["hhs_care"].shift(1) + df["cbp_transfers_out"]
                           - df["hhs_discharges"])
    out["hhs_residual"] = df["hhs_care"] - out["hhs_expected"]
    out["comparable"] = gap.eq(1)
    return out


def implied_hhs_admissions(df: pd.DataFrame) -> pd.Series:
    """Derived HHS entries = change in HHS care + discharges (1-day gaps only).

    Independent of the CBP columns. It equals transfers into HHS only if the
    HHS stock-flow identity holds with no other adjustments.
    """
    gap = ensure_gap_days(df)
    value = df["hhs_care"].diff() + df["hhs_discharges"]
    return value.where(gap.eq(1))


def gap_category(gap: pd.Series) -> pd.Series:
    bins = [0, 1, 3, np.inf]
    labels = ["1 day", "2-3 days", "4+ days"]
    return pd.cut(gap, bins=bins, labels=labels)


def summarize_residuals(residual: pd.Series, groups: pd.Series | None = None) -> pd.DataFrame:
    """Distribution of residuals, optionally by a grouping series."""
    frame = pd.DataFrame({"residual": residual})
    frame["group"] = "all" if groups is None else groups.astype(str)
    frame = frame.dropna(subset=["residual"])
    columns = ["n", "mean", "median", "median_abs", "p90_abs", "share_within_10"]
    rows = {}
    for group, s in frame.groupby("group", observed=True)["residual"]:
        a = s.abs()
        rows[group] = [int(s.size), s.mean(), s.median(), a.median(), a.quantile(0.9), (a <= 10).mean()]
    return pd.DataFrame.from_dict(rows, orient="index", columns=columns).rename_axis("group")


STAGES = {
    "CBP": ("cbp_custody", "cbp_intake", "cbp_transfers_out"),
    "HHS": ("hhs_care", "cbp_transfers_out", "hhs_discharges"),
}


def decompose_stock_change(df: pd.DataFrame, by: pd.Series | None = None) -> pd.DataFrame:
    """Split each stage's stock change into one-day pairs and reporting-gap pairs.

    For consecutive reports (t-1, t) with all values present:
      change = (entries(t) - exits(t)) + residual
    On one-day pairs the residual is the unexplained part of a single day. On
    gap pairs, flows are only reported for day t, so the residual also holds
    the net movement of the unreported days in between. A large gap residual
    means reported-day flows cannot be used to infer stock direction.
    """
    gap = ensure_gap_days(df)
    groups = by if by is not None else pd.Series("all", index=df.index)
    rows = []
    for stage, (stock, entry, exit_) in STAGES.items():
        change = df[stock].diff()
        flows = df[entry] - df[exit_]
        ok = change.notna() & flows.notna()
        resid = change - flows
        for key, idx in groups.groupby(groups).groups.items():
            m = pd.Series(False, index=df.index)
            m[idx] = True
            one, multi = m & ok & gap.eq(1), m & ok & gap.gt(1)
            missing_days = (gap[multi] - 1).sum()
            rows.append({
                "stage": stage, "group": key,
                "pairs_one_day": int(one.sum()), "pairs_gap": int(multi.sum()),
                "unreported_days_in_gaps": int(missing_days),
                "change_total": change[m & ok].sum(),
                "change_one_day": change[one].sum(), "flows_one_day": flows[one].sum(),
                "residual_one_day": resid[one].sum(),
                "change_gap": change[multi].sum(), "flows_gap_report_day": flows[multi].sum(),
                "residual_gap": resid[multi].sum(),
                "residual_per_unreported_day": resid[multi].sum() / missing_days if missing_days else np.nan,
            })
    return pd.DataFrame(rows)


def stage_summary(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Opening/closing stocks, period flows and unexplained change per stage.

    Entry/exit totals and per-day means use only records where both flows are
    present. Flows on the first record are treated as already reflected in the
    opening snapshot, so reconciliation uses flows from the second record
    onward. Unexplained change therefore includes flows on unreported days.
    """
    if df.empty:
        return {}

    def first_last(col: str) -> tuple[float, float, object, object]:
        s = df[["date", col]].dropna()
        if s.empty:
            return np.nan, np.nan, None, None
        return (float(s[col].iloc[0]), float(s[col].iloc[-1]),
                s["date"].iloc[0], s["date"].iloc[-1])

    after_first = df.iloc[1:]
    result: dict[str, dict[str, float]] = {}
    for stage, stock, entry, exit_ in (
        ("CBP", "cbp_custody", "cbp_intake", "cbp_transfers_out"),
        ("HHS", "hhs_care", "cbp_transfers_out", "hhs_discharges"),
    ):
        opening, closing, d0, d1 = first_last(stock)
        # Entries and exits over the same records only, so they are comparable.
        aligned = df[[entry, exit_]].dropna()
        entries_total = aligned[entry].sum() if len(aligned) else np.nan
        exits_total = aligned[exit_].sum() if len(aligned) else np.nan
        in_after = after_first[entry].sum(min_count=1)
        out_after = after_first[exit_].sum(min_count=1)
        incomplete = int((~df[[stock, entry, exit_]].notna().all(axis=1)).sum())
        expected = opening + in_after - out_after if incomplete == 0 else np.nan
        result[stage] = {
            "opening": opening,
            "closing": closing,
            "opening_date": d0,
            "closing_date": d1,
            "entries_total": float(entries_total) if pd.notna(entries_total) else np.nan,
            "exits_total": float(exits_total) if pd.notna(exits_total) else np.nan,
            "entries_per_day": aligned[entry].mean() if len(aligned) else np.nan,
            "exits_per_day": aligned[exit_].mean() if len(aligned) else np.nan,
            "net_change": closing - opening,
            "expected_closing": expected,
            "unexplained_change": closing - expected if pd.notna(expected) else np.nan,
            "records_with_flows": len(aligned),
            "records_incomplete": incomplete,
        }
    return result
