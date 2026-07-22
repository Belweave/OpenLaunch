"""Provider-neutral, read-only data source tools for the agent harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time as monotonic_time
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from openlaunch.env import (
    DATA_SOURCE_CONNECTIONS,
    DATA_SOURCE_CONNECTIONS_FILE,
    DATA_SOURCE_MAX_QUERY_CHARACTERS,
    DATA_SOURCE_MAX_RESULT_BYTES,
    DATA_SOURCE_MAX_ROWS,
    DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
    ENABLE_DATA_SOURCE_TOOLS,
    ENABLE_SQL_DATABASE_TOOL,
    SQL_DATABASE_URL,
)
from openlaunch.models.groups import Groups
from openlaunch.models.control_plane import ControlPlanes, get_persisted_runtime_connections
from openlaunch.tools.data_source_sdk import (
    AdapterCapabilities,
    DataSourceAdapter,
    get_adapter,
    register_adapter,
)
from openlaunch.tools.sql_policy import SQLPolicyError, apply_result_governance, enforce_sql_policy
from openlaunch.utils.access_control import has_access
from openlaunch.utils.tool_executor import tool_annotations

log = logging.getLogger(__name__)

SUPPORTED_DATA_SOURCE_TYPES = frozenset({"postgresql", "sql_server", "azure_sql", "snowflake", "redis"})
SQL_DATA_SOURCE_TYPES = frozenset({"postgresql", "sql_server", "azure_sql", "snowflake"})
_TYPE_ALIASES = {
    "postgres": "postgresql",
    "mssql": "sql_server",
    "sqlserver": "sql_server",
    "azure-sql": "azure_sql",
}
_ALLOWED_SQL_STATEMENTS = {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC"}
_CONNECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_engines: dict[str, tuple[str, Engine]] = {}
_engine_lock = threading.Lock()


class DataSourceValidationError(ValueError):
    """Raised when a request violates the data source tool policy."""


def _normalize_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return _TYPE_ALIASES.get(normalized, normalized)


def _parse_connection_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, dict):
        document = [dict(value, id=key) for key, value in document.items() if isinstance(value, dict)]
    if not isinstance(document, list):
        raise ValueError("Data source configuration must be a JSON list or object")
    return [item for item in document if isinstance(item, dict)]


def get_configured_data_sources() -> list[dict[str, Any]]:
    """Load and validate named connections without exposing their credentials."""
    configured: list[dict[str, Any]] = []

    if DATA_SOURCE_CONNECTIONS.strip():
        try:
            configured.extend(_parse_connection_document(json.loads(DATA_SOURCE_CONNECTIONS)))
        except Exception as exc:
            log.error("Data source environment configuration is invalid (%s)", type(exc).__name__)

    if DATA_SOURCE_CONNECTIONS_FILE.strip():
        try:
            document = json.loads(Path(DATA_SOURCE_CONNECTIONS_FILE).read_text(encoding="utf-8"))
            configured.extend(_parse_connection_document(document))
        except Exception as exc:
            log.error("Data source configuration file is invalid (%s)", type(exc).__name__)

    try:
        configured.extend(get_persisted_runtime_connections())
    except Exception as exc:
        # Startup/migration windows must not break environment-backed deployments.
        log.debug("Persisted data-source registry is unavailable (%s)", type(exc).__name__)

    # Backward compatibility for the original single-PostgreSQL integration.
    if ENABLE_SQL_DATABASE_TOOL and SQL_DATABASE_URL.strip():
        configured.append(
            {
                "id": "default-postgresql",
                "type": "postgresql",
                "description": "Default PostgreSQL database",
                "url": SQL_DATABASE_URL,
                # The legacy global feature permission remains the access gate.
                "access_grants": [
                    {
                        "principal_type": "user",
                        "principal_id": "*",
                        "permission": "read",
                    }
                ],
            }
        )

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in configured:
        connection_id = str(item.get("id") or "").strip()
        source_type = _normalize_type(item.get("type"))
        if not item.get("enabled", True):
            continue
        if not _CONNECTION_ID_PATTERN.fullmatch(connection_id):
            log.error("Ignoring a data source with an invalid connection ID")
            continue
        if connection_id in seen_ids:
            log.error("Ignoring duplicate data source connection ID %s", connection_id)
            continue
        if source_type not in SUPPORTED_DATA_SOURCE_TYPES:
            log.error("Ignoring data source %s with unsupported type %s", connection_id, source_type)
            continue
        seen_ids.add(connection_id)
        result.append({**item, "id": connection_id, "type": source_type})
    return result


def is_data_sources_configured() -> bool:
    return (ENABLE_DATA_SOURCE_TOOLS or ENABLE_SQL_DATABASE_TOOL) and bool(get_configured_data_sources())


async def _user_can_access(connection: dict[str, Any], user: dict[str, Any] | None) -> bool:
    user = user or {}
    if user.get("role") == "admin":
        return True
    scope_type = connection.get('scope_type', 'instance')
    scope_id = str(connection.get('scope_id', '*'))
    if scope_type != 'instance' and str(user.get(scope_type) or user.get(f'{scope_type}_id') or '') != scope_id:
        return False
    user_id = str(user.get("id") or "")
    if not user_id:
        return False
    group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}
    return await has_access(
        user_id,
        "read",
        connection.get("access_grants") or [],
        group_ids,
    )


async def _get_authorized_data_source(
    connection_id: str,
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    connection = next(
        (item for item in get_configured_data_sources() if item["id"] == connection_id),
        None,
    )
    if connection is None or not await _user_can_access(connection, user):
        # Deliberately do not distinguish missing from unauthorized connections.
        raise DataSourceValidationError("The requested data source is not available.")
    profile_grants = (user or {}).get('tool_profile_data_source_grants')
    if profile_grants is not None and connection_id not in profile_grants:
        raise DataSourceValidationError("The requested data source is not available.")
    return connection


def _scan_sql(query: str) -> tuple[str, bool]:
    """Find the first keyword and top-level statement separators."""
    cleaned: list[str] = []
    separators: list[int] = []
    index = 0
    state = "normal"
    dollar_tag = ""

    while index < len(query):
        char = query[index]
        following = query[index + 1] if index + 1 < len(query) else ""
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
                index += 1
            elif char == "/" and following == "*":
                state = "block_comment"
                cleaned.extend((" ", " "))
                index += 1
            elif char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", query[index:])
                if match:
                    dollar_tag = match.group(0)
                    state = "dollar"
                    cleaned.extend(" " * len(dollar_tag))
                    index += len(dollar_tag) - 1
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
                index += 1
            elif char == "'":
                state = "normal"
        elif state == "double":
            cleaned.append(" ")
            if char == '"' and following == '"':
                cleaned.append(" ")
                index += 1
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
                index += 1
                state = "normal"
        elif state == "dollar":
            if query.startswith(dollar_tag, index):
                cleaned.extend(" " * len(dollar_tag))
                index += len(dollar_tag) - 1
                state = "normal"
            else:
                cleaned.append(" ")
        index += 1

    if state in {"single", "double", "block_comment", "dollar"}:
        raise DataSourceValidationError("The query contains an unterminated quote or comment.")

    scanned = "".join(cleaned)
    match = re.search(r"[A-Za-z]+", scanned)
    keyword = match.group(0).upper() if match else ""
    meaningful = scanned.rstrip()
    internal_separator = any(position < len(meaningful) - 1 for position in separators)
    return keyword, internal_separator


def validate_readonly_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise DataSourceValidationError("A query is required.")
    if "\x00" in query:
        raise DataSourceValidationError("The query contains an invalid character.")
    if len(query) > DATA_SOURCE_MAX_QUERY_CHARACTERS:
        raise DataSourceValidationError(f"The query exceeds the {DATA_SOURCE_MAX_QUERY_CHARACTERS}-character limit.")
    keyword, has_internal_separator = _scan_sql(query)
    if keyword not in _ALLOWED_SQL_STATEMENTS:
        raise DataSourceValidationError(
            "Only read-only SELECT, WITH, EXPLAIN, SHOW, and DESCRIBE statements are allowed."
        )
    if has_internal_separator:
        raise DataSourceValidationError("Only one statement is allowed per tool call.")
    return query.strip().removesuffix(";").rstrip()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time, UUID)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary data: {len(value)} bytes>"
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _fit_tabular_result(
    connection: dict[str, Any],
    columns: list[str],
    rows: list[Any],
    truncated: bool,
) -> str:
    payload = {
        "connection_id": connection["id"],
        "type": connection["type"],
        "columns": columns,
        "rows": [[_json_value(value) for value in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while payload["rows"] and len(encoded.encode("utf-8")) > DATA_SOURCE_MAX_RESULT_BYTES:
        payload["rows"].pop()
        payload["row_count"] = len(payload["rows"])
        payload["truncated"] = True
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > DATA_SOURCE_MAX_RESULT_BYTES:
        return json.dumps({"error": "The data source result exceeds the configured output limit.", "truncated": True})
    return encoded


def _postgres_engine_url(url: str) -> str:
    value = url.strip()
    if value.startswith("postgres://"):
        value = f'postgresql://{value.removeprefix("postgres://")}'
    if value.startswith("postgresql://"):
        value = f'postgresql+psycopg://{value.removeprefix("postgresql://")}'
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError("PostgreSQL data sources require a PostgreSQL URL")
    return value


def _get_postgres_engine(connection: dict[str, Any]) -> Engine:
    url = _postgres_engine_url(str(connection.get("url") or ""))
    fingerprint = hashlib.sha256(url.encode()).hexdigest()
    connection_id = connection["id"]
    cached = _engines.get(connection_id)
    if cached and cached[0] == fingerprint:
        return cached[1]
    with _engine_lock:
        cached = _engines.get(connection_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            cached[1].dispose()
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=0,
            pool_recycle=300,
            connect_args={"connect_timeout": max(1, DATA_SOURCE_QUERY_TIMEOUT_SECONDS)},
        )
        _engines[connection_id] = (fingerprint, engine)
        return engine


def _execute_postgresql(connection: dict[str, Any], query: str, parameters: dict[str, Any] | None = None) -> str:
    max_rows = DATA_SOURCE_MAX_ROWS
    timeout_ms = DATA_SOURCE_QUERY_TIMEOUT_SECONDS * 1000
    with _get_postgres_engine(connection).connect() as database:
        with database.begin():
            database.execute(text("SET TRANSACTION READ ONLY"))
            database.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            result = database.execution_options(stream_results=True, max_row_buffer=max_rows + 1).execute(
                text(query), parameters or {}
            )
            if not result.returns_rows:
                raise DataSourceValidationError("The statement did not return rows.")
            columns = list(result.keys())
            rows = result.fetchmany(max_rows + 1)
    return _fit_tabular_result(connection, columns, rows[:max_rows], len(rows) > max_rows)


def _mssql_connection_string(connection: dict[str, Any]) -> str:
    config = connection.get("config") or {}
    value = str(connection.get("connection_string") or connection.get("url") or "").strip()
    if value:
        return value
    required = ("server", "database", "user", "password")
    if any(not config.get(key) for key in required):
        raise ValueError("SQL Server data source configuration is incomplete")

    def quote(item: Any) -> str:
        return "{" + str(item).replace("}", "}}") + "}"

    return ";".join(
        [
            "DRIVER={ODBC Driver 18 for SQL Server}",
            f"SERVER={quote(config['server'])}",
            f"DATABASE={quote(config['database'])}",
            f"UID={quote(config['user'])}",
            f"PWD={quote(config['password'])}",
            f"Encrypt={config.get('encrypt', 'yes')}",
            f"TrustServerCertificate={config.get('trust_server_certificate', 'no')}",
        ]
    )


def _execute_mssql(connection: dict[str, Any], query: str, parameters: tuple[Any, ...] = ()) -> str:
    import pyodbc

    max_rows = DATA_SOURCE_MAX_ROWS
    database = pyodbc.connect(
        _mssql_connection_string(connection),
        timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
        readonly=True,
        autocommit=False,
    )
    try:
        database.timeout = DATA_SOURCE_QUERY_TIMEOUT_SECONDS
        cursor = database.cursor()
        cursor.timeout = DATA_SOURCE_QUERY_TIMEOUT_SECONDS
        cursor.execute(f"SET LOCK_TIMEOUT {DATA_SOURCE_QUERY_TIMEOUT_SECONDS * 1000}")
        cursor.execute(query, *parameters)
        if cursor.description is None:
            raise DataSourceValidationError("The statement did not return rows.")
        columns = [str(item[0]) for item in cursor.description]
        rows = cursor.fetchmany(max_rows + 1)
        database.rollback()
        return _fit_tabular_result(connection, columns, rows[:max_rows], len(rows) > max_rows)
    finally:
        database.close()


def _snowflake_config(connection: dict[str, Any]) -> dict[str, Any]:
    config = dict(connection.get("config") or {})
    if not config:
        raise ValueError("Snowflake data source configuration is incomplete")
    session_parameters = dict(config.get("session_parameters") or {})
    session_parameters.update(
        {
            "QUERY_TAG": "openlaunch_data_source_tool",
            "STATEMENT_TIMEOUT_IN_SECONDS": DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
        }
    )
    config.update(
        {
            "session_parameters": session_parameters,
            "login_timeout": DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
            "network_timeout": DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
            "application": "OpenLaunch",
        }
    )
    return config


def _execute_snowflake(connection: dict[str, Any], query: str, parameters: dict[str, Any] | None = None) -> str:
    import snowflake.connector

    max_rows = DATA_SOURCE_MAX_ROWS
    with snowflake.connector.connect(**_snowflake_config(connection)) as database:
        database.autocommit(False)
        cursor = database.cursor()
        try:
            cursor.execute(query, parameters or {}, timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS)
            if cursor.description is None:
                raise DataSourceValidationError("The statement did not return rows.")
            columns = [str(item[0]) for item in cursor.description]
            rows = cursor.fetchmany(max_rows + 1)
            database.rollback()
            return _fit_tabular_result(connection, columns, rows[:max_rows], len(rows) > max_rows)
        finally:
            cursor.close()


def _execute_sql_provider(
    connection: dict[str, Any],
    query: str,
    parameters: dict[str, Any] | tuple[Any, ...] | None = None,
) -> str:
    if connection["type"] == "postgresql":
        return _execute_postgresql(connection, query, parameters if isinstance(parameters, dict) else None)
    if connection["type"] in {"sql_server", "azure_sql"}:
        return _execute_mssql(connection, query, parameters if isinstance(parameters, tuple) else ())
    if connection["type"] == "snowflake":
        return _execute_snowflake(connection, query, parameters if isinstance(parameters, dict) else None)
    raise DataSourceValidationError("This data source does not support SQL queries.")


def _execute_sql(
    connection: dict[str, Any],
    query: str,
    parameters: dict[str, Any] | tuple[Any, ...] | None = None,
) -> str:
    return get_adapter(connection['type']).query(connection, query=query, parameters=parameters)


@tool_annotations(
    read_only=True,
    destructive=False,
    idempotent=True,
    external_network=False,
    approval_required=False,
    timeout_seconds=10,
)
async def list_data_sources(__user__: dict[str, Any] | None = None) -> str:
    """
    List the named data sources available to the current user. This returns only safe
    identifiers, types, and descriptions; credentials and connection details are never exposed.

    :return: JSON describing the available data source connection IDs
    """
    result = []
    for connection in get_configured_data_sources():
        if await _user_can_access(connection, __user__):
            result.append(
                {
                    "connection_id": connection["id"],
                    "type": connection["type"],
                    "description": str(connection.get("description") or ""),
                }
            )
    return json.dumps({"data_sources": result}, ensure_ascii=False)


@tool_annotations(
    read_only=True,
    destructive=False,
    idempotent=True,
    external_network=True,
    approval_required=False,
    timeout_seconds=30,
)
async def inspect_data_source(
    connection_id: str,
    catalog: str | None = None,
    namespace: str | None = None,
    object_name: str | None = None,
    __user__: dict[str, Any] | None = None,
) -> str:
    """
    Inspect tables and columns in a named SQL data source. Use list_data_sources first.
    Catalog means database and namespace means schema. Results are read-only and capped.

    :param connection_id: Named data source ID returned by list_data_sources
    :param catalog: Optional database or catalog filter
    :param namespace: Optional schema filter
    :param object_name: Optional table or view filter
    :return: JSON rows describing available columns
    """
    try:
        connection = await _get_authorized_data_source(connection_id, __user__)
        if connection["type"] == "redis":
            raise DataSourceValidationError("Use read_redis_data_source to inspect Redis keys.")

        query = """
            SELECT table_catalog, table_schema, table_name,
                   column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE (:catalog IS NULL OR table_catalog = :catalog)
              AND (:namespace IS NULL OR table_schema = :namespace)
              AND (:object_name IS NULL OR table_name = :object_name)
            ORDER BY table_catalog, table_schema, table_name, ordinal_position
        """
        parameters: dict[str, Any] | tuple[Any, ...] = {
            "catalog": catalog,
            "namespace": namespace,
            "object_name": object_name,
        }
        if connection["type"] in {"sql_server", "azure_sql"}:
            query = query.replace(":catalog", "?").replace(":namespace", "?").replace(":object_name", "?")
            parameters = (catalog, catalog, namespace, namespace, object_name, object_name)
        elif connection["type"] == "snowflake":
            query = (
                query.replace(":catalog", "%(catalog)s")
                .replace(":namespace", "%(namespace)s")
                .replace(":object_name", "%(object_name)s")
            )
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_sql, connection, query, parameters),
            timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS + 2,
        )
        return result
    except DataSourceValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except TimeoutError:
        return json.dumps({"error": "The data source inspection timed out."})
    except Exception as exc:
        log.error("Data source inspection failed (%s)", type(exc).__name__)
        return json.dumps({"error": "The data source inspection failed."})


@tool_annotations(
    read_only=True,
    destructive=False,
    idempotent=True,
    external_network=True,
    approval_required=False,
    timeout_seconds=35,
)
async def query_data_source(
    connection_id: str,
    query: str,
    __user__: dict[str, Any] | None = None,
    __metadata__: dict[str, Any] | None = None,
) -> str:
    """
    Run one read-only query against a named SQL data source. Supports PostgreSQL,
    SQL Server, Azure SQL, and Snowflake. Use list_data_sources and inspect_data_source
    first. Mutations and multiple statements are blocked.

    :param connection_id: Named data source ID returned by list_data_sources
    :param query: One read-only SQL statement
    :return: JSON with columns, rows, row_count, and truncation status
    """
    started = monotonic_time.monotonic()
    started_epoch = int(monotonic_time.time())
    connection = None
    decision = None
    status = 'denied'
    result = ''
    try:
        connection = await _get_authorized_data_source(connection_id, __user__)
        if connection["type"] not in SQL_DATA_SOURCE_TYPES:
            raise DataSourceValidationError("This data source does not support SQL queries.")
        if len(query) > DATA_SOURCE_MAX_QUERY_CHARACTERS:
            raise DataSourceValidationError(f"The query exceeds the {DATA_SOURCE_MAX_QUERY_CHARACTERS}-character limit.")
        decision = enforce_sql_policy(query, connection['type'], connection.get('policy'))
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_sql, connection, decision.query),
            timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS + 2,
        )
        result = apply_result_governance(result, connection.get('policy'))
        status = 'success'
        return result
    except SQLPolicyError as exc:
        return json.dumps({'error': str(exc)}, ensure_ascii=False)
    except DataSourceValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except TimeoutError:
        status = 'timeout'
        return json.dumps({"error": "The data source query timed out."})
    except Exception as exc:
        status = 'error'
        log.error("Data source query failed (%s)", type(exc).__name__)
        return json.dumps({"error": "The data source query failed."})
    finally:
        if connection:
            try:
                payload = json.loads(result) if result else {}
                policy = connection.get('policy') or {}
                await ControlPlanes.append_query_audit(
                    {
                        'actor_id': str((__user__ or {}).get('id') or ''),
                        'connection_id': connection_id,
                        'provider_type': connection['type'],
                        'request_id': str(
                            (__metadata__ or {}).get('request_id')
                            or (__metadata__ or {}).get('message_id')
                            or ''
                        ),
                        'tool_call_id': str((__metadata__ or {}).get('tool_call_id') or ''),
                        'objects': list(decision.objects) if decision else [],
                        'policy_decision': 'allow' if decision else 'deny',
                        'query_fingerprint': decision.fingerprint if decision else '',
                        'raw_sql': query if policy.get('audit_raw_sql', False) else None,
                        'started_at': started_epoch,
                        'ended_at': int(monotonic_time.time()),
                        'duration_ms': int((monotonic_time.monotonic() - started) * 1000),
                        'row_count': int(payload.get('row_count') or 0),
                        'result_bytes': len(result.encode('utf-8')) if result else 0,
                        'status': status,
                    }
                )
            except Exception as exc:
                log.error('Query audit append failed (%s)', type(exc).__name__)


RedisReadOperation = Literal[
    "scan",
    "type",
    "ttl",
    "get",
    "hget",
    "hgetall",
    "lrange",
    "smembers",
    "zrange",
    "xrange",
]


def _execute_redis_read(
    connection: dict[str, Any],
    operation: RedisReadOperation,
    key: str | None,
    pattern: str | None,
    field: str | None,
    start: int,
    stop: int | None,
) -> str:
    import redis

    url = str(connection.get("url") or "").strip()
    if not url.startswith(("redis://", "rediss://", "unix://")):
        raise ValueError("Redis data sources require a Redis URL")
    max_rows = DATA_SOURCE_MAX_ROWS
    client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
        socket_timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS,
    )
    try:
        columns: list[str]
        rows: list[Any]
        truncated = False
        if operation == "scan":
            values = []
            for value in client.scan_iter(match=pattern or "*", count=min(max_rows + 1, 1000)):
                values.append(value)
                if len(values) > max_rows:
                    break
            columns, rows = ["key"], [[value] for value in values[:max_rows]]
            truncated = len(values) > max_rows
        else:
            if not key:
                raise DataSourceValidationError(f"A key is required for Redis {operation}.")
            if operation == "type":
                columns, rows = ["type"], [[client.type(key)]]
            elif operation == "ttl":
                columns, rows = ["ttl_seconds"], [[client.ttl(key)]]
            elif operation == "get":
                columns, rows = ["value"], [[client.get(key)]]
            elif operation == "hget":
                if not field:
                    raise DataSourceValidationError("A field is required for Redis hget.")
                columns, rows = ["field", "value"], [[field, client.hget(key, field)]]
            elif operation == "hgetall":
                values = []
                for item in client.hscan_iter(key, count=min(max_rows + 1, 1000)):
                    values.append(item)
                    if len(values) > max_rows:
                        break
                columns, rows = ["field", "value"], values[:max_rows]
                truncated = len(values) > max_rows
            elif operation == "lrange":
                end = min(stop if stop is not None else start + max_rows, start + max_rows)
                values = client.lrange(key, start, end)
                columns, rows = ["index", "value"], [
                    [start + offset, value] for offset, value in enumerate(values[:max_rows])
                ]
                truncated = len(values) > max_rows
            elif operation == "smembers":
                values = []
                for value in client.sscan_iter(key, count=min(max_rows + 1, 1000)):
                    values.append(value)
                    if len(values) > max_rows:
                        break
                columns, rows = ["member"], [[value] for value in values[:max_rows]]
                truncated = len(values) > max_rows
            elif operation == "zrange":
                end = min(stop if stop is not None else start + max_rows, start + max_rows)
                values = client.zrange(key, start, end, withscores=True)
                columns, rows = ["member", "score"], values[:max_rows]
                truncated = len(values) > max_rows
            elif operation == "xrange":
                values = client.xrange(key, count=max_rows + 1)
                columns, rows = ["entry_id", "fields"], values[:max_rows]
                truncated = len(values) > max_rows
            else:
                raise DataSourceValidationError("The Redis read operation is not supported.")
        return _fit_tabular_result(connection, columns, rows, truncated)
    finally:
        client.close()


@tool_annotations(
    read_only=True,
    destructive=False,
    idempotent=True,
    external_network=True,
    approval_required=False,
    timeout_seconds=35,
)
async def read_redis_data_source(
    connection_id: str,
    operation: RedisReadOperation,
    key: str | None = None,
    pattern: str | None = None,
    field: str | None = None,
    start: int = 0,
    stop: int | None = None,
    __user__: dict[str, Any] | None = None,
) -> str:
    """
    Perform a constrained read-only operation on a named Redis data source. Supported
    operations are scan, type, ttl, get, hget, hgetall, lrange, smembers, zrange, and xrange.
    Arbitrary Redis commands and all mutations are unavailable.

    :param connection_id: Named Redis data source ID returned by list_data_sources
    :param operation: Allowed read operation
    :param key: Exact Redis key, required except for scan
    :param pattern: Optional SCAN pattern; used only by scan
    :param field: Hash field; required by hget
    :param start: Start index for list and sorted-set ranges
    :param stop: Optional inclusive stop index for list and sorted-set ranges
    :return: Capped JSON tabular result
    """
    try:
        connection = await _get_authorized_data_source(connection_id, __user__)
        if connection["type"] != "redis":
            raise DataSourceValidationError("This data source is not Redis.")
        return await asyncio.wait_for(
            asyncio.to_thread(
                get_adapter(connection['type']).query,
                connection,
                operation,
                key=key,
                pattern=pattern,
                field=field,
                start=max(0, start),
                stop=stop,
            ),
            timeout=DATA_SOURCE_QUERY_TIMEOUT_SECONDS + 2,
        )
    except DataSourceValidationError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except TimeoutError:
        return json.dumps({"error": "The Redis read timed out."})
    except Exception as exc:
        log.error("Redis data source read failed (%s)", type(exc).__name__)
        return json.dumps({"error": "The Redis data source read failed."})


class SQLDataSourceAdapter(DataSourceAdapter):
    provider_types = ('postgresql', 'sql_server', 'azure_sql', 'snowflake')
    capabilities = AdapterCapabilities(
        inspect_schema=True,
        query=True,
        supports_cancellation=True,
        supports_explain=True,
    )

    def test_connection(self, connection: dict[str, Any]) -> None:
        _execute_sql_provider(connection, 'SELECT 1')

    def query(self, connection: dict[str, Any], operation: str = '', **options) -> str:
        query = str(options.get('query') or operation)
        return _execute_sql_provider(connection, query, options.get('parameters'))


class RedisDataSourceAdapter(DataSourceAdapter):
    provider_types = ('redis',)
    capabilities = AdapterCapabilities(
        bounded_operations=(
            'scan', 'type', 'ttl', 'get', 'hget', 'hgetall', 'lrange', 'smembers', 'zrange', 'xrange'
        ),
        supports_cancellation=True,
    )

    def test_connection(self, connection: dict[str, Any]) -> None:
        _execute_redis_read(connection, 'scan', None, '*', None, 0, 0)

    def query(self, connection: dict[str, Any], operation: str, **options) -> str:
        return _execute_redis_read(
            connection,
            operation,
            options.get('key'),
            options.get('pattern'),
            options.get('field'),
            options.get('start', 0),
            options.get('stop'),
        )


register_adapter(SQLDataSourceAdapter())
register_adapter(RedisDataSourceAdapter())
