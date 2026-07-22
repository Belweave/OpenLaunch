"""Read-only SQL database tools for the model-agnostic agent harness."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from openlaunch.env import (
    ENABLE_SQL_DATABASE_TOOL,
    SQL_DATABASE_MAX_QUERY_CHARACTERS,
    SQL_DATABASE_MAX_RESULT_BYTES,
    SQL_DATABASE_MAX_ROWS,
    SQL_DATABASE_QUERY_TIMEOUT_SECONDS,
    SQL_DATABASE_URL,
)

log = logging.getLogger(__name__)

_ALLOWED_STATEMENTS = {"SELECT", "WITH", "EXPLAIN", "SHOW"}
_engine: Engine | None = None
_engine_url: str | None = None
_engine_lock = threading.Lock()


class SQLToolValidationError(ValueError):
    """Raised when a model-generated query violates the SQL tool policy."""


def is_sql_database_configured() -> bool:
    """Return whether the SQL tool is explicitly enabled and has a connection URL."""
    return ENABLE_SQL_DATABASE_TOOL and bool(SQL_DATABASE_URL.strip())


def _postgres_engine_url(url: str) -> str:
    """Normalize PostgreSQL URLs onto the bundled psycopg v3 driver."""
    value = url.strip()
    if value.startswith("postgres://"):
        value = f'postgresql://{value.removeprefix("postgres://")}'
    if value.startswith("postgresql://"):
        value = f'postgresql+psycopg://{value.removeprefix("postgresql://")}'
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError(
            "The SQL database tool currently supports PostgreSQL URLs only"
        )
    return value


def _get_engine() -> Engine:
    """Create one small, lazy pool without ever logging the configured URL."""
    global _engine, _engine_url

    url = _postgres_engine_url(SQL_DATABASE_URL)
    if _engine is not None and _engine_url == url:
        return _engine

    with _engine_lock:
        if _engine is not None and _engine_url == url:
            return _engine
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=0,
            pool_recycle=300,
            connect_args={
                "connect_timeout": max(1, SQL_DATABASE_QUERY_TIMEOUT_SECONDS)
            },
        )
        _engine_url = url
        return _engine


def _scan_sql(query: str) -> tuple[str, bool]:
    """Return the first keyword and whether an internal statement separator exists.

    The scanner ignores quoted strings, quoted identifiers, PostgreSQL dollar strings,
    and comments. A final semicolon is accepted; any other top-level semicolon is not.
    PostgreSQL read-only transactions remain the authoritative write protection.
    """
    cleaned: list[str] = []
    separators: list[int] = []
    i = 0
    state = "normal"
    dollar_tag = ""

    while i < len(query):
        char = query[i]
        following = query[i + 1] if i + 1 < len(query) else ""

        if state == "normal":
            if char == "'":
                state = "single"
                cleaned.append(" ")
            elif char == '"':
                state = "double"
                cleaned.append(" ")
            elif char == "-" and following == "-":
                state = "line_comment"
                cleaned.extend((" ", " "))
                i += 1
            elif char == "/" and following == "*":
                state = "block_comment"
                cleaned.extend((" ", " "))
                i += 1
            elif char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", query[i:])
                if match:
                    dollar_tag = match.group(0)
                    state = "dollar"
                    cleaned.extend(" " * len(dollar_tag))
                    i += len(dollar_tag) - 1
                else:
                    cleaned.append(char)
            else:
                cleaned.append(char)
                if char == ";":
                    separators.append(len(cleaned) - 1)
        elif state == "single":
            cleaned.append(" ")
            if char == "'" and following == "'":
                cleaned.append(" ")
                i += 1
            elif char == "'":
                state = "normal"
        elif state == "double":
            cleaned.append(" ")
            if char == '"' and following == '"':
                cleaned.append(" ")
                i += 1
            elif char == '"':
                state = "normal"
        elif state == "line_comment":
            cleaned.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            cleaned.append(" ")
            if char == "*" and following == "/":
                cleaned.append(" ")
                i += 1
                state = "normal"
        elif state == "dollar":
            if query.startswith(dollar_tag, i):
                cleaned.extend(" " * len(dollar_tag))
                i += len(dollar_tag) - 1
                state = "normal"
            else:
                cleaned.append(" ")
        i += 1

    if state in {"single", "double", "block_comment", "dollar"}:
        raise SQLToolValidationError(
            "The SQL query contains an unterminated quote or comment."
        )

    scanned = "".join(cleaned)
    first_keyword_match = re.search(r"[A-Za-z]+", scanned)
    first_keyword = first_keyword_match.group(0).upper() if first_keyword_match else ""

    meaningful = scanned.rstrip()
    internal_separator = any(position < len(meaningful) - 1 for position in separators)
    return first_keyword, internal_separator


def validate_readonly_query(query: str) -> str:
    """Validate and normalize one model-generated read-only SQL statement."""
    if not isinstance(query, str) or not query.strip():
        raise SQLToolValidationError("A SQL query is required.")
    if "\x00" in query:
        raise SQLToolValidationError("The SQL query contains an invalid character.")
    if len(query) > SQL_DATABASE_MAX_QUERY_CHARACTERS:
        raise SQLToolValidationError(
            f"The SQL query exceeds the {SQL_DATABASE_MAX_QUERY_CHARACTERS}-character limit."
        )

    first_keyword, has_internal_separator = _scan_sql(query)
    if first_keyword not in _ALLOWED_STATEMENTS:
        raise SQLToolValidationError(
            "Only read-only SELECT, WITH, EXPLAIN, and SHOW statements are allowed."
        )
    if has_internal_separator:
        raise SQLToolValidationError("Only one SQL statement is allowed per tool call.")
    return query.strip().removesuffix(";").rstrip()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time, UUID)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary data: {len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _fit_result(payload: dict[str, Any]) -> str:
    """Keep tool output within the configured prompt budget by dropping tail rows."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= SQL_DATABASE_MAX_RESULT_BYTES:
        return encoded

    payload["truncated"] = True
    rows = payload.get("rows", [])
    while rows:
        rows.pop()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= SQL_DATABASE_MAX_RESULT_BYTES:
            return encoded

    return json.dumps(
        {
            "error": "The database result metadata exceeds the configured output limit.",
            "truncated": True,
        },
        separators=(",", ":"),
    )


def _execute_readonly(query: str, parameters: dict[str, Any] | None = None) -> str:
    engine = _get_engine()
    max_rows = max(1, SQL_DATABASE_MAX_ROWS)
    timeout_ms = max(1, SQL_DATABASE_QUERY_TIMEOUT_SECONDS) * 1000

    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            result = connection.execution_options(
                stream_results=True, max_row_buffer=max_rows + 1
            ).execute(text(query), parameters or {})
            if not result.returns_rows:
                raise SQLToolValidationError("The SQL statement did not return rows.")

            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [[_json_value(value) for value in row] for row in fetched[:max_rows]]

    return _fit_result(
        {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    )


async def query_sql_database(query: str) -> str:
    """
    Run one read-only SQL query against the configured enterprise PostgreSQL database.
    Use list_sql_database_schema first when table or column names are unknown. Only
    SELECT, WITH, EXPLAIN, and SHOW are accepted; writes and multiple statements are blocked.

    :param query: One read-only PostgreSQL query
    :return: JSON with columns, rows, row_count, and whether the result was truncated
    """
    if not is_sql_database_configured():
        return json.dumps({"error": "The SQL database tool is not configured."})

    try:
        normalized = validate_readonly_query(query)
        return await asyncio.wait_for(
            asyncio.to_thread(_execute_readonly, normalized),
            timeout=max(1, SQL_DATABASE_QUERY_TIMEOUT_SECONDS) + 2,
        )
    except SQLToolValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except TimeoutError:
        log.warning("SQL database tool query timed out")
        return json.dumps({"error": "The database query timed out."})
    except Exception as exc:
        # Do not log the driver message: it can contain SQL text or connection details.
        log.error("SQL database tool query failed (%s)", type(exc).__name__)
        return json.dumps({"error": "The database query failed."})


async def list_sql_database_schema(
    schema_name: str | None = None,
    table_name: str | None = None,
) -> str:
    """
    List tables and columns in the configured enterprise PostgreSQL database.
    Call this before querying when the database structure is unknown. System schemas
    are excluded. Results are subject to the same row and output limits as queries.

    :param schema_name: Optional schema name to inspect; omit to inspect all application schemas
    :param table_name: Optional table name to inspect; omit to list columns across tables
    :return: JSON rows describing schema, table, column, data type, and nullability
    """
    if not is_sql_database_configured():
        return json.dumps({"error": "The SQL database tool is not configured."})

    query = """
        SELECT table_schema, table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
          AND (:schema IS NULL OR table_schema = :schema)
          AND (:table IS NULL OR table_name = :table)
        ORDER BY table_schema, table_name, ordinal_position
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _execute_readonly,
                query,
                {"schema": schema_name, "table": table_name},
            ),
            timeout=max(1, SQL_DATABASE_QUERY_TIMEOUT_SECONDS) + 2,
        )
    except TimeoutError:
        log.warning("SQL database schema inspection timed out")
        return json.dumps({"error": "The database schema inspection timed out."})
    except Exception as exc:
        # Do not log the driver message: it can contain SQL text or connection details.
        log.error("SQL database schema inspection failed (%s)", type(exc).__name__)
        return json.dumps({"error": "The database schema inspection failed."})
