"""Parse, clean, flag and screen the records, keeping an audit trail.

Two analysis views are produced from the same records:

* ``records``  - every valid record exactly as reported, plus quality flags;
* ``screened`` - identical, except values with evidence of not belonging to
  their reporting date are set to missing (never to zero, never imputed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import (CBP_COLUMNS, DATE_FORMATS, DEFAULT_QUALITY, HHS_COLUMNS, LABELS,
                    VALUE_COLUMNS, QualityConfig)
from src.data_loader import LoadResult
from src.data_validation import (SUSPECT_COPY, UNRESOLVED, QualityReport,
                                 run_quality_checks)

logger = logging.getLogger(__name__)

WEEKDAY_ORDER = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


@dataclass
class PreparedData:
    records: pd.DataFrame
    screened: pd.DataFrame
    quality: QualityReport
    audit_log: list[dict] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)
    source_name: str = ""

    def view(self, screened: bool = True) -> pd.DataFrame:
        return self.screened if screened else self.records

    def audit_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.audit_log, columns=["step", "decision", "rows_affected", "rationale"])


def parse_dates(values: pd.Series) -> pd.Series:
    """Parse dates trying known formats first, then a flexible fallback."""
    text = values.astype(str).str.strip()
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    for fmt in DATE_FORMATS:
        todo = parsed.isna() & text.ne("")
        if not todo.any():
            break
        parsed[todo] = pd.to_datetime(text[todo], format=fmt, errors="coerce")
    todo = parsed.isna() & text.ne("")
    if todo.any():
        parsed[todo] = pd.to_datetime(text[todo], errors="coerce", format="mixed")
    return parsed.dt.normalize()


def parse_counts(values: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Convert count text such as '2,484' to numbers.

    Returns (numeric values, mask of non-blank entries that were not numeric).
    Blank cells become missing; nothing is converted to zero.
    """
    text = values.astype(str).str.strip()
    cleaned = text.str.replace(",", "", regex=False).str.replace(" ", "", regex=False)
    numeric = pd.to_numeric(cleaned, errors="coerce").astype(float)
    invalid = numeric.isna() & text.ne("")
    return numeric, invalid


def _file_order(dates: pd.Series) -> str:
    valid = dates.dropna()
    if valid.is_monotonic_increasing:
        return "ascending (oldest first)"
    if valid.is_monotonic_decreasing:
        return "descending (newest first)"
    return "unordered"


def add_calendar_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["gap_days"] = df["date"].diff().dt.days
    df["weekday"] = pd.Categorical(df["date"].dt.day_name(), categories=WEEKDAY_ORDER, ordered=True)
    df["is_sunday"] = df["date"].dt.dayofweek.eq(6)
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.to_period("M")
    df["quarter"] = df["date"].dt.to_period("Q")
    # Reporting weeks end on Saturday so each Sunday-Thursday block stays together.
    df["week"] = df["date"].dt.to_period("W-SAT")
    return df


def prepare_dataset(load: LoadResult, qcfg: QualityConfig = DEFAULT_QUALITY,
                    start: str | pd.Timestamp | None = None) -> PreparedData:
    """Full cleaning pipeline from raw strings to records + screened view.

    ``start`` restricts the analysis window: records dated earlier are dropped
    (and logged) before any quality check, so checks describe the window only.
    """
    audit: list[dict] = []

    def log(step: str, decision: str, rows: int, rationale: str) -> None:
        audit.append({"step": step, "decision": decision, "rows_affected": int(rows),
                      "rationale": rationale})
        logger.info("%s: %s (%d rows)", step, decision, rows)

    raw = load.raw
    footnotes = [h for h in load.column_map.values() if any(ch in h for ch in "*†‡")]
    log("load", f"Read {load.rows_in_file} data rows from {load.source_name}", load.rows_in_file,
        "Raw values kept as text for traceability (source_row = file line number).")
    if load.blank_rows_dropped:
        log("blank rows", "Dropped fully blank rows", load.blank_rows_dropped,
            "Rows with no value in any column carry no observation.")
    log("schema", "Mapped headers to standard names: "
        + "; ".join(f"{k} <- '{v}'" for k, v in load.column_map.items()), 0,
        "Original headers preserved in column_map.")

    df = pd.DataFrame({"source_row": raw["source_row"]})
    df["date"] = parse_dates(raw["date"])
    invalid_dates = raw.loc[df["date"].isna(), "date"].tolist()
    invalid_values: dict[str, list[str]] = {}
    non_integer: dict[str, int] = {}
    for col in VALUE_COLUMNS:
        df[col], bad = parse_counts(raw[col])
        if bad.any():
            invalid_values[col] = raw.loc[bad, col].tolist()
            log("parse values", f"{LABELS[col]}: non-numeric values set to missing", bad.sum(),
                "Unparseable values are not assumed to be zero.")
        frac = df[col].notna() & (df[col] % 1 != 0)
        non_integer[col] = int(frac.sum())
    log("parse values", "Removed thousands separators and converted counts to numbers", 0,
        "HHS care values are formatted like '2,484'.")
    order = _file_order(df["date"])

    if invalid_dates:
        log("parse dates", "Excluded rows with unparseable dates", len(invalid_dates),
            "A record without a valid date cannot be placed on the timeline.")
    df = df.loc[df["date"].notna()]
    if start is not None:
        before = df["date"] < pd.Timestamp(start)
        if before.any():
            log("analysis window", f"Dropped records dated before {pd.Timestamp(start):%Y-%m-%d}",
                before.sum(), "Analysis window chosen by the user; earlier records are not analysed.")
        df = df.loc[~before]

    exact_dupes = df.duplicated(subset=["date", *VALUE_COLUMNS], keep="first")
    if exact_dupes.any():
        log("duplicates", "Collapsed exact duplicate rows", exact_dupes.sum(),
            "Identical rows add no information.")
    df = df.loc[~exact_dupes]
    conflicting = df["date"].duplicated(keep=False)
    conflict_dates = sorted(df.loc[conflicting, "date"].dt.strftime("%Y-%m-%d").unique())
    if conflict_dates:
        log("duplicates", "Kept first occurrence (file order) of dates with conflicting values",
            df["date"].duplicated().sum(), "Analysis requires one record per date; flagged as critical.")
    df = df.loc[~df["date"].duplicated(keep="first")]
    if df.empty:
        raise ValueError("The file contains no records with a valid date; nothing to analyse.")

    df = df.sort_values("date").reset_index(drop=True)
    log("ordering", f"Sorted chronologically (file was {order})", len(df), "Time-series operations need ascending order.")
    df = add_calendar_fields(df)

    parse_info = {
        "rows_in_file": load.rows_in_file,
        "blank_rows": load.blank_rows_dropped,
        "footnote_headers": footnotes,
        "invalid_dates": invalid_dates,
        "invalid_values": invalid_values,
        "non_integer": non_integer,
        "exact_duplicate_rows": int(exact_dupes.sum()),
        "conflicting_duplicate_dates": conflict_dates,
        "file_order": order,
    }
    quality = run_quality_checks(df, parse_info, qcfg)

    dup = quality.duplicates
    df["cbp_duplicate_status"] = dup.row_status
    df["cbp_duplicate_run_length"] = dup.run_length
    df["cbp_duplicate_partner_dates"] = dup.partner_dates
    df["hhs_possible_carry_forward"] = quality.carry_forward
    df["cbp_possible_carry_forward"] = quality.cbp_carry_forward

    mask_status = {SUSPECT_COPY} | ({UNRESOLVED} if qcfg.mask_unresolved_duplicates else set())
    cbp_mask = df["cbp_duplicate_status"].isin(mask_status)
    if qcfg.mask_cbp_carry_forward:
        cbp_mask |= df["cbp_possible_carry_forward"]
    hhs_mask = df["hhs_possible_carry_forward"] if qcfg.mask_hhs_carry_forward \
        else pd.Series(False, index=df.index)

    screened = df.copy()
    screened.loc[cbp_mask, CBP_COLUMNS] = np.nan
    screened.loc[hhs_mask, HHS_COLUMNS] = np.nan
    df["cbp_values_screened_out"] = cbp_mask
    df["hhs_values_screened_out"] = hhs_mask
    screened["cbp_values_screened_out"] = cbp_mask
    screened["hhs_values_screened_out"] = hhs_mask

    log("screening", "CBP values set to missing in screened view (suspected displaced copies"
        + (", unresolved duplicates" if qcfg.mask_unresolved_duplicates else "")
        + (", possible carry-forward)" if qcfg.mask_cbp_carry_forward else ")"), cbp_mask.sum(),
        "Exact multi-record repeats of CBP values whose transfers do not reconcile with HHS stock "
        "changes at their reported dates. HHS values on these rows are kept.")
    if hhs_mask.any():
        log("screening", "HHS values set to missing in screened view (possible carry-forward)",
            hhs_mask.sum(), "Stock and discharges identical to the previous report suggest a stale snapshot.")
    log("missing days", "No calendar days were added or zero-filled", int(quality.summary["missing_calendar_days"]),
        "There is no evidence that unreported days had zero activity.")

    return PreparedData(records=df, screened=screened, quality=quality, audit_log=audit,
                        column_map=load.column_map, source_name=load.source_name)
