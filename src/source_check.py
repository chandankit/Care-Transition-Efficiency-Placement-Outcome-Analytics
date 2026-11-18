"""Cross-check the analysed records against a reference copy of the published dataset.

Every value cell is compared with the reference on the same date. Nothing is
modified: the result documents where the file departs from the source, so that
a screening decision (for example, masking duplicated CBP values) can be
confirmed or refuted externally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import VALUE_COLUMNS

# Cell-level status values
MATCH = "match"
BOTH_MISSING = "both missing"
NOT_IN_REFERENCE = "value not in reference"   # reference cell blank, file has a value
MISSING_IN_FILE = "missing in file"           # file cell blank, reference has a value
DIFFERENT = "different value"
CELL_STATUSES = (MATCH, BOTH_MISSING, NOT_IN_REFERENCE, MISSING_IN_FILE, DIFFERENT)


@dataclass
class SourceCheck:
    reference_name: str
    cells: pd.DataFrame                 # date, column, file_value, reference_value, status (+ group)
    summary: pd.DataFrame               # column x status counts
    dates_only_in_file: list[pd.Timestamp] = field(default_factory=list)
    dates_only_in_reference: list[pd.Timestamp] = field(default_factory=list)  # within the file's range

    @property
    def departures(self) -> pd.DataFrame:
        """Cells where the file and the reference disagree."""
        return self.cells[~self.cells["status"].isin([MATCH, BOTH_MISSING])]

    @property
    def identical(self) -> bool:
        return self.departures.empty and not self.dates_only_in_file and not self.dates_only_in_reference

    def by_group(self, group_col: str, columns: list[str] | None = None) -> pd.DataFrame:
        """Status counts per value of a record flag (e.g. the duplicated-sequence status)."""
        cells = self.cells if columns is None else self.cells[self.cells["column"].isin(columns)]
        if group_col not in cells:
            return pd.DataFrame()
        return (cells.pivot_table(index=group_col, columns="status", values="date", aggfunc="size", fill_value=0)
                .reindex(columns=[s for s in CELL_STATUSES if s in set(cells["status"])], fill_value=0))

    def status_spans(self) -> pd.DataFrame:
        """First date, last date and cell count for each column and non-matching status."""
        dep = self.departures
        if dep.empty:
            return pd.DataFrame(columns=["column", "status", "cells", "first_date", "last_date"])
        return (dep.groupby(["column", "status"], sort=False)["date"]
                .agg(cells="size", first_date="min", last_date="max").reset_index())


def compare_with_reference(records: pd.DataFrame, reference: pd.DataFrame,
                           columns: list[str] | None = None, reference_name: str = "reference",
                           group_col: str | None = "cbp_duplicate_status") -> SourceCheck:
    """Compare value cells of ``records`` with ``reference`` on matching dates.

    Only reference dates inside the records' date range are considered, so a
    reference that extends further (an older or newer publication) is not
    reported as having extra dates.
    """
    columns = list(columns or VALUE_COLUMNS)
    keep = ["date", *columns] + ([group_col] if group_col and group_col in records else [])
    file = records[keep]
    ref = reference[["date", *columns]]
    if not file.empty:
        ref = ref[(ref["date"] >= file["date"].min()) & (ref["date"] <= file["date"].max())]
    merged = file.merge(ref, on="date", how="outer", suffixes=("_file", "_ref"), indicator=True)
    both = merged[merged["_merge"] == "both"]

    parts = []
    for col in columns:
        a, b = both[f"{col}_file"], both[f"{col}_ref"]
        status = np.select([a.isna() & b.isna(), a.isna(), b.isna(), a == b],
                           [BOTH_MISSING, MISSING_IN_FILE, NOT_IN_REFERENCE, MATCH], DIFFERENT)
        part = pd.DataFrame({"date": both["date"].to_numpy(), "column": col,
                             "file_value": a.to_numpy(), "reference_value": b.to_numpy(), "status": status})
        if group_col and group_col in both:
            part[group_col] = both[group_col].to_numpy()
        parts.append(part)
    cells = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["date", "column", "file_value", "reference_value", "status"])

    summary = (cells.pivot_table(index="column", columns="status", values="date", aggfunc="size", fill_value=0)
               .reindex(index=columns, columns=list(CELL_STATUSES), fill_value=0)) if len(cells) else \
        pd.DataFrame(0, index=columns, columns=list(CELL_STATUSES))
    return SourceCheck(
        reference_name=reference_name, cells=cells, summary=summary,
        dates_only_in_file=list(merged.loc[merged["_merge"] == "left_only", "date"]),
        dates_only_in_reference=list(merged.loc[merged["_merge"] == "right_only", "date"]),
    )
