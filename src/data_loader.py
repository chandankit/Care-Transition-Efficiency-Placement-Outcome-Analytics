"""Read the source file and map its headers to standard internal names.

Values are kept as raw strings here; parsing happens in ``preprocessing`` so
that every conversion decision can be audited against the original text.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Union

import pandas as pd

from config import COLUMN_SPECS

logger = logging.getLogger(__name__)

Source = Union[str, Path, IO[bytes], IO[str]]


class SchemaError(ValueError):
    """Raised when required columns are missing or ambiguous."""


@dataclass
class LoadResult:
    raw: pd.DataFrame                      # standardised names, raw string values
    column_map: dict[str, str]             # standard name -> original header
    source_name: str
    rows_in_file: int                      # data rows excluding the header
    blank_rows_dropped: int
    extra_columns: list[str] = field(default_factory=list)


def normalize_header(name: str) -> str:
    """Lower-case, drop footnote markers (*, †) and collapse whitespace."""
    cleaned = re.sub(r"[*†‡]+", "", str(name))
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def map_columns(columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Match source headers to the expected schema.

    Returns (standard -> original header, unmatched extra headers).
    Raises SchemaError listing every missing or duplicated required column.
    """
    normalized: dict[str, list[str]] = {}
    for col in columns:
        normalized.setdefault(normalize_header(col), []).append(col)

    mapping: dict[str, str] = {}
    problems: list[str] = []
    for spec in COLUMN_SPECS:
        matches = normalized.get(normalize_header(spec.source), [])
        if not matches:
            problems.append(f"missing column '{spec.source}'")
        elif len(matches) > 1:
            problems.append(f"column '{spec.source}' appears {len(matches)} times")
        else:
            mapping[spec.standard] = matches[0]
    if problems:
        raise SchemaError("Input schema is invalid: " + "; ".join(problems))

    used = set(mapping.values())
    extras = [c for c in columns if c not in used]
    return mapping, extras


def load_raw(source: Source) -> LoadResult:
    """Load a CSV file (path or file-like) without converting any values."""
    if isinstance(source, (str, Path)):
        source_name = str(source)
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Data file not found: {path}")
        handle: Source = path
    else:
        source_name = getattr(source, "name", "uploaded file")
        content = source.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8-sig")
        handle = io.StringIO(content)

    try:
        frame = pd.read_csv(handle, dtype=str, keep_default_na=False, encoding_errors="replace")
    except pd.errors.EmptyDataError:
        raise SchemaError("The file is empty: it has no header row and no data.") from None
    frame.columns = [str(c).lstrip("﻿") for c in frame.columns]
    mapping, extras = map_columns(list(frame.columns))

    rows_in_file = len(frame)
    # File line number (header is line 1) is kept for traceability.
    frame.insert(0, "source_row", frame.index + 2)
    stripped = frame[list(mapping.values())].apply(lambda c: c.str.strip())
    blank = (stripped == "").all(axis=1)
    frame = frame.loc[~blank].copy()

    raw = frame[["source_row"]].copy()
    for standard, original in mapping.items():
        raw[standard] = frame[original].str.strip()
    raw = raw.reset_index(drop=True)

    logger.info("Loaded %s: %d rows in file, %d fully blank rows dropped",
                source_name, rows_in_file, int(blank.sum()))
    return LoadResult(raw=raw, column_map=mapping, source_name=source_name,
                      rows_in_file=rows_in_file, blank_rows_dropped=int(blank.sum()),
                      extra_columns=extras)
