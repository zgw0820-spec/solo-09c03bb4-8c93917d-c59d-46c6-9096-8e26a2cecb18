"""Configuration knobs (all overridable via environment variables)."""

from __future__ import annotations

import os


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


#: A single SQL statement (full SELECT or one incremental statement) that runs
#: longer than this is *undeterminable*, never evidence in the reduction.
STATEMENT_TIMEOUT_SECONDS = _float("STATEMENT_TIMEOUT_SECONDS", 1.0)

#: Hard cap on rows a query may produce.  Prevents a cross-join explosion from
#: exhausting memory.  Exceeding the cap is reported as undeterminable.
MAX_RESULT_ROWS = _int("MAX_RESULT_ROWS", 10_000)

#: Hard cap on the number of distinct trial inputs evaluated for one request.
MAX_TRIALS = _int("MAX_TRIALS", 400)

#: Wall-clock budget for one request.  Exceeding it ends the reduction with
#: status ``undetermined`` (never "complete").
MAX_REQUEST_SECONDS = _float("MAX_REQUEST_SECONDS", 120.0)

#: SQLite per-connection value size limit (bytes).  A larger value is rejected
#: as a resource limit rather than consuming unbounded memory.
MAX_VALUE_BYTES = _int("MAX_VALUE_BYTES", 1_000_000)

#: Maximum database size per trial, in 4 KiB pages (default 64 MiB).  Loading
#: the initial full product into ``result`` must fit; exceeding this is
#: reported as undeterminable, never as a difference.
MAX_DB_PAGES = _int("MAX_DB_PAGES", 16_384)

#: Maximum accepted length of each submitted SQL string.
MAX_SQL_CHARS = _int("MAX_SQL_CHARS", 100_000)
