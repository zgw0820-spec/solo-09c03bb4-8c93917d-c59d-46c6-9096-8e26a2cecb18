"""SQL safety policy.

Submitted SQL is never trusted.  Safety is enforced in two layers:

1. A textual pre-check with clear error messages: statement count, leading
   keyword, forbidden DDL/admin keywords, and the *target table* of every
   write statement.
2. A SQLite **authorizer** installed for the duration of each statement.  The
   authorizer is the real boundary: it allows reads of ``orders``/``refunds``
   (and, for incremental SQL, ``batch_changes`` and ``result``) and writes
   only to ``result``; everything else is denied, including ATTACH, DDL,
   PRAGMA, transaction control and extension loading.
"""

from __future__ import annotations

import re
import sqlite3

FULL = "full"
INCREMENTAL = "incremental"

SOURCE_TABLES = frozenset({"orders", "refunds"})
RESULT_TABLE = "result"
CDC_TABLE = "batch_changes"

# Words that must never appear in either SQL string.  Note that REPLACE and
# ROLLBACK are intentionally absent: they also occur inside legitimate
# conflict clauses (``INSERT OR REPLACE``, ``OR ROLLBACK``).  Standalone
# ``REPLACE INTO result`` is still safe and is handled by the target check;
# transaction statements are denied by the authorizer.
_FORBIDDEN_WORDS = {
    "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE", "PRAGMA",
    "LOAD_EXTENSION", "CREATE", "DROP", "ALTER", "TRIGGER",
    "BEGIN", "COMMIT", "SAVEPOINT", "RELEASE",
}

_DML = {"INSERT", "UPDATE", "DELETE", "REPLACE"}

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<string>'(?:''|[^'])*')
  | (?P<qident>"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\])
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<other>.)
    """,
    re.S | re.X,
)


class SqlPolicyError(ValueError):
    """The submitted SQL is not allowed to run."""


# ---------------------------------------------------------------------------
# Tokenization helpers
# ---------------------------------------------------------------------------

def tokenize(sql: str) -> list[tuple[str, str]]:
    """Whitespace/comment-free tokens; strings and quoted identifiers kept."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(sql):
        match = _TOKEN_RE.match(sql, pos)
        if not match:  # pragma: no cover - regex always consumes one char
            break
        kind = match.lastgroup or "other"
        if kind not in ("ws", "comment"):
            tokens.append((kind, match.group(0)))
        pos = match.end()
    return tokens


def _word(tok: tuple[str, str] | None) -> str:
    if tok is None or tok[0] != "word":
        return ""
    return tok[1].upper()


def _identifier(tok: tuple[str, str] | None) -> str:
    if tok is None:
        return ""
    kind, value = tok
    if kind == "word":
        return value.lower()
    if kind == "qident":
        if value.startswith("["):
            return value[1:-1].lower()
        quote = value[0]
        return value[1:-1].replace(quote * 2, quote).lower()
    return ""


def first_keyword(tokens: list[tuple[str, str]]) -> str:
    for kind, value in tokens:
        if kind == "word":
            return value.upper()
    return ""


def words(tokens: list[tuple[str, str]]) -> set[str]:
    return {value.upper() for kind, value in tokens if kind == "word"}


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, ignoring literals and comments.

    Uses :func:`sqlite3.complete_statement` on accumulated chunks so that
    semicolons inside string literals or comments do not split.
    Comment-only or empty chunks are dropped, so a trailing ``-- note`` is
    not mistaken for an (empty) statement.
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";":
            if sqlite3.complete_statement(buffer):
                _maybe_add_statement(statements, buffer)
                buffer = ""
    _maybe_add_statement(statements, buffer)
    return statements


def _maybe_add_statement(statements: list[str], chunk: str) -> None:
    piece = chunk.strip()
    if not piece.strip("; \t\r\n"):
        return
    if not tokenize(piece):  # only whitespace / comments
        return
    statements.append(piece)


# ---------------------------------------------------------------------------
# Textual checks
# ---------------------------------------------------------------------------

def _check_common(sql: str, label: str) -> None:
    if not sql or not sql.strip():
        raise SqlPolicyError(f"{label} 不能为空")
    tokens = tokenize(sql)
    bad = sorted(words(tokens) & _FORBIDDEN_WORDS)
    if bad:
        allowed = "只允许读取 orders/refunds" + (
            "并维护 result" if label == INCREMENTAL else ""
        )
        raise SqlPolicyError(f"{label} 含禁止关键字: {', '.join(bad)}（{allowed}）")


def _write_target(tokens: list[tuple[str, str]], start: int) -> str:
    """Extract the target table of the DML statement beginning at ``start``."""
    keyword = _word(tokens[start])
    index = start + 1
    if keyword in ("INSERT", "REPLACE", "UPDATE"):
        # Optional ``OR <conflict-action>``.
        if _word(tokens[index] if index < len(tokens) else None) == "OR":
            index += 2
    if keyword in ("INSERT", "REPLACE"):
        if _word(tokens[index] if index < len(tokens) else None) != "INTO":
            raise SqlPolicyError("增量 SQL 的 INSERT/REPLACE 语句缺少 INTO")
        index += 1
    elif keyword == "DELETE":
        if _word(tokens[index] if index < len(tokens) else None) != "FROM":
            raise SqlPolicyError("增量 SQL 的 DELETE 语句缺少 FROM")
        index += 1

    if index >= len(tokens):
        return ""
    name = _identifier(tokens[index])
    # Support ``schema.table``: keep the last component.
    while (
        index + 2 < len(tokens)
        and tokens[index + 1] == ("other", ".")
    ):
        index += 2
        name = _identifier(tokens[index])
    return name


_DML_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE"}

# Verbs that may follow a CTE list (``WITH ... INSERT/UPDATE/DELETE``).
_CTE_VERBS = {"SELECT", "VALUES"} | _DML_VERBS


def _looks_like_verb(tokens: list[tuple[str, str]], index: int) -> bool:
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if following is None:
        return True
    kind, value = following
    if kind == "other" and value in "(.,=<>+-*/%|&":
        # ``replace(...)`` / ``x.update`` style: an identifier, not a verb.
        return False
    return True


def _top_level_dml(tokens: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """Positions of top-level DML verbs (depth 0), ignoring CTE bodies."""
    found: list[tuple[int, str]] = []
    depth = 0
    for index, (kind, value) in enumerate(tokens):
        if kind == "other":
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(0, depth - 1)
            continue
        if depth != 0 or kind != "word":
            continue
        keyword = value.upper()
        if keyword in _DML_VERBS and _looks_like_verb(tokens, index):
            previous = _word(tokens[index - 1]) if index >= 1 else ""
            # ``INSERT OR REPLACE`` uses REPLACE as a conflict action.
            if keyword == "REPLACE" and previous == "OR":
                continue
            # ``ON CONFLICT ... DO UPDATE SET ...`` upsert clause.
            if keyword == "UPDATE" and previous == "DO":
                continue
            found.append((index, keyword))
    return found


def _top_level_cte_verb(tokens: list[tuple[str, str]]) -> tuple[int, str] | None:
    """The verb following a ``WITH`` CTE list, at depth 0."""
    depth = 0
    for index, (kind, value) in enumerate(tokens):
        if kind == "other":
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(0, depth - 1)
            continue
        if depth != 0 or kind != "word":
            continue
        keyword = value.upper()
        if keyword in _CTE_VERBS and _looks_like_verb(tokens, index):
            return index, keyword
    return None


def check_full(sql: str) -> None:
    _check_common(sql, FULL)
    statements = split_statements(sql)
    if len(statements) != 1:
        raise SqlPolicyError("full_select 必须是单条 SELECT 语句")
    tokens = tokenize(statements[0])
    first = first_keyword(tokens)
    if first not in {"SELECT", "WITH", "VALUES"}:
        raise SqlPolicyError(
            f"full_select 必须是单条 SELECT/WITH 语句，收到 {first or '空'}"
        )
    if _top_level_dml(tokens):
        raise SqlPolicyError("full_select 中不允许出现写语句")
    if RESULT_TABLE.upper() in words(tokens):
        raise SqlPolicyError("full_select 不得读取 result 表（只能读 orders/refunds）")
    if CDC_TABLE.upper() in words(tokens):
        raise SqlPolicyError(
            "full_select 不得读取 batch_changes 表（只能读 orders/refunds）"
        )
    if words(tokens) & _SYSTEM_TABLE_WORDS:
        raise SqlPolicyError("full_select 不得读取系统表")


_SYSTEM_TABLE_WORDS = {"SQLITE_MASTER", "SQLITE_SCHEMA", "SQLITE_TEMP_MASTER"}


def check_incremental(sql: str) -> None:
    _check_common(sql, INCREMENTAL)
    statements = split_statements(sql)
    if not statements:
        raise SqlPolicyError("incremental 不能为空")
    wrote_result = False
    for statement in statements:
        tokens = tokenize(statement)
        first = first_keyword(tokens)
        if not first:
            raise SqlPolicyError("incremental 含空语句")
        dml = _top_level_dml(tokens)
        if not dml:
            if first in {"SELECT", "WITH", "VALUES"}:
                continue
            raise SqlPolicyError(f"增量 SQL 出现不允许的语句: {first}")
        # A single statement may contain at most one top-level DML verb
        # (SQLite does not allow mixing, so more than one means an attempt to
        # smuggle a second statement past the splitter).
        if len(dml) > 1:
            raise SqlPolicyError("增量 SQL 的单条语句包含多个写操作")
        dml_index, dml_kind = dml[0]
        target = _write_target(tokens, dml_index)
        if target != RESULT_TABLE:
            raise SqlPolicyError(
                f"增量 SQL 只能写 {RESULT_TABLE} 表，"
                f"{dml_kind} 的目标是 {target or '未知'}"
            )
        wrote_result = True
    if not wrote_result:
        raise SqlPolicyError("增量 SQL 至少需要一条写入 result 的语句")


# ---------------------------------------------------------------------------
# Authorizer
# ---------------------------------------------------------------------------

_DENIED_ACTIONS = {
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_SAVEPOINT,
    sqlite3.SQLITE_TRANSACTION,
}

_WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
}

_SYSTEM_TABLES = {
    "sqlite_master",
    "sqlite_schema",
    "sqlite_temp_master",
    "sqlite_temp_schema",
}

#: Real tables in every trial database.  Any other name seen by the authorizer
#: is a CTE / derived-table alias; SQLite reports CTE references as READs too,
#: so we allow unknown names and let SQLite itself fail for missing tables.
_REAL_TABLES = SOURCE_TABLES | {RESULT_TABLE, CDC_TABLE}

_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit",
                     "fts3_tokenizer"}

#: Non-deterministic functions make a trial unreproducible.  The task requires
#: every round to rebuild from the *same* initial state, so these are denied.
_NON_DETERMINISTIC_FUNCTIONS = {
    "random",
    "randomblob",
    "datetime",
    "date",
    "time",
    "strftime",
    "julianday",
    "unixepoch",
    "current_date",
    "current_time",
    "current_timestamp",
    "changes",
    "last_insert_rowid",
    "total_changes",
}


class _Authorizer:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.tables: set[str] = set()
        self.violation: str | None = None

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or arg1 or "").lower()
            if name in _DENIED_FUNCTIONS:
                return self._deny(f"调用不允许的函数 {name}")
            if name in _NON_DETERMINISTIC_FUNCTIONS:
                return self._deny(
                    f"调用非确定性函数 {name}（试验要求可重现）"
                )
            return sqlite3.SQLITE_OK

        if action in _DENIED_ACTIONS:
            return self._deny(f"不允许的操作（authorizer action={action}）")

        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK

        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            if not table:
                return sqlite3.SQLITE_OK
            if table in _SYSTEM_TABLES:
                return self._deny(f"不允许读取系统表 {table}")
            if table.startswith("pragma_"):
                return self._deny(
                    f"不允许读取 {table}（PRAGMA 表值函数被禁用）"
                )
            allowed = set(SOURCE_TABLES)
            if self.mode == INCREMENTAL:
                allowed |= {CDC_TABLE, RESULT_TABLE}
            if table in allowed:
                self.tables.add(table)
                return sqlite3.SQLITE_OK
            # Unknown names are CTE / subquery aliases; SQLite resolves real
            # table names itself, so a missing table still errors naturally.
            if table not in _REAL_TABLES:
                return sqlite3.SQLITE_OK
            return self._deny(f"{self.mode} SQL 不允许读取表 {table}")

        if action in _WRITE_ACTIONS:
            table = (arg1 or "").lower()
            if self.mode != INCREMENTAL:
                return self._deny("full_select 必须是只读的")
            if table != RESULT_TABLE:
                return self._deny(f"增量 SQL 只能写 {RESULT_TABLE}，尝试写 {table}")
            self.tables.add(table)
            return sqlite3.SQLITE_OK

        return self._deny(f"不允许的操作（authorizer action={action}）")

    def _deny(self, message: str) -> int:
        if self.violation is None:
            self.violation = message
        return sqlite3.SQLITE_DENY


class AuthorizerContext:
    """Context manager installing the policy authorizer on a connection."""

    def __init__(self, conn: sqlite3.Connection, mode: str) -> None:
        self.conn = conn
        self.mode = mode
        self.authorizer: _Authorizer | None = None

    def __enter__(self) -> _Authorizer:
        self.authorizer = _Authorizer(self.mode)
        self.conn.set_authorizer(self.authorizer)
        return self.authorizer

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.set_authorizer(None)
        if exc is not None and self.authorizer and self.authorizer.violation:
            raise SqlPolicyError(
                f"SQL 违反执行策略: {self.authorizer.violation}"
            ) from exc
        return None
