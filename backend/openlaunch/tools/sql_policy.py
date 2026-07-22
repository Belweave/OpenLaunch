"""Dialect-aware SQL classification and reusable data-governance hooks."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from typing import Any
import json

from sqlglot import exp, parse
from sqlglot.errors import ParseError


class SQLPolicyError(ValueError):
    pass


DIALECTS = {
    "postgresql": "postgres",
    "sql_server": "tsql",
    "azure_sql": "tsql",
    "snowflake": "snowflake",
}

_MUTATING_KEYS = {
    "insert",
    "update",
    "delete",
    "merge",
    "create",
    "drop",
    "alter",
    "truncate",
    "copy",
    "grant",
    "revoke",
    "transaction",
    "commit",
    "rollback",
    "set",
    "use",
    "execute",
    "command",
    "into",
    "load_data",
}

_UNSAFE_FUNCTIONS = {
    "BENCHMARK",
    "DBMS_LOCK.SLEEP",
    "DBLINK",
    "DBLINK_CONNECT",
    "HTTP_GET",
    "HTTP_POST",
    "LO_EXPORT",
    "LO_IMPORT",
    "OPENROWSET",
    "OPENDATASOURCE",
    "PG_LS_DIR",
    "PG_READ_BINARY_FILE",
    "PG_READ_FILE",
    "PG_SLEEP",
    "SYSTEM$WAIT",
}


@dataclass(frozen=True)
class SQLPolicyDecision:
    query: str
    dialect: str
    objects: tuple[str, ...]
    columns: tuple[str, ...]
    fingerprint: str
    join_count: int


def _matches(value: str, patterns: list[str]) -> bool:
    lowered = value.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def _table_name(table: exp.Table) -> str:
    parts = [table.catalog, table.db, table.name]
    return ".".join(part for part in parts if part)


def _fingerprint(expression: exp.Expression, dialect: str) -> str:
    canonical = expression.sql(dialect=dialect, pretty=False, normalize=True)
    canonical = re.sub(r"'(?:''|[^'])*'", "?", canonical)
    canonical = re.sub(r"\b\d+(?:\.\d+)?\b", "?", canonical)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enforce_sql_policy(query: str, provider_type: str, policy: dict[str, Any] | None = None) -> SQLPolicyDecision:
    """Parse one statement, reject unsafe AST nodes, and enforce object/column policy."""
    policy = policy or {}
    dialect = DIALECTS.get(provider_type)
    if dialect is None:
        raise SQLPolicyError("The SQL dialect is unsupported.")
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise SQLPolicyError("A valid query is required.")
    try:
        statements = [statement for statement in parse(query, read=dialect) if statement is not None]
    except ParseError as exc:
        raise SQLPolicyError("The query could not be classified safely.") from exc
    if len(statements) != 1:
        raise SQLPolicyError("Only one statement is allowed.")
    expression = statements[0]
    if not isinstance(expression, (exp.Query, exp.Describe, exp.Show)):
        raise SQLPolicyError("Only read-only query statements are allowed.")

    for node in expression.walk():
        if getattr(node, "key", "").lower() in _MUTATING_KEYS:
            raise SQLPolicyError("The query contains an unsafe operation.")
        if isinstance(node, exp.Anonymous) and node.name.upper() in _UNSAFE_FUNCTIONS:
            raise SQLPolicyError("The query contains a blocked function.")

    objects = tuple(dict.fromkeys(_table_name(table) for table in expression.find_all(exp.Table)))
    allow_objects = list(policy.get("object_allowlist") or [])
    deny_objects = list(policy.get("object_denylist") or [])
    for object_name in objects:
        if deny_objects and _matches(object_name, deny_objects):
            raise SQLPolicyError("The query references a denied object.")
        if allow_objects and not _matches(object_name, allow_objects):
            raise SQLPolicyError("The query references an object outside the allowlist.")

    columns = tuple(
        dict.fromkeys(
            ".".join(part for part in (column.table, column.name) if part) for column in expression.find_all(exp.Column)
        )
    )
    allow_columns = list(policy.get("column_allowlist") or [])
    deny_columns = list(policy.get("column_denylist") or [])
    for column_name in columns:
        if deny_columns and _matches(column_name, deny_columns):
            raise SQLPolicyError("The query references a denied column.")
        if allow_columns and not _matches(column_name, allow_columns):
            raise SQLPolicyError("The query references a column outside the allowlist.")
    if (allow_columns or deny_columns) and any(isinstance(node, exp.Star) for node in expression.walk()):
        raise SQLPolicyError("Wildcard columns are not allowed when column policy is active.")

    join_count = sum(1 for _ in expression.find_all(exp.Join))
    max_joins = max(0, int(policy.get("max_joins", 12)))
    max_scans = max(1, int(policy.get("max_scans", 24)))
    if join_count > max_joins or len(objects) > max_scans:
        raise SQLPolicyError("The query exceeds configured cost safeguards.")

    # Predicates are admin-authored SQL fragments and are parsed in the same dialect.
    predicates = policy.get("row_predicates") or {}
    applicable = [predicate for name, predicate in predicates.items() if any(_matches(obj, [name]) for obj in objects)]
    if applicable:
        for select in expression.find_all(exp.Select):
            for predicate in applicable:
                try:
                    condition = parse(str(predicate), read=dialect)[0]
                except Exception as exc:
                    raise SQLPolicyError("A configured row policy is invalid.") from exc
                select.where(condition, append=True, copy=False)

    normalized = expression.sql(dialect=dialect, pretty=False)
    return SQLPolicyDecision(
        query=normalized,
        dialect=dialect,
        objects=objects,
        columns=columns,
        fingerprint=_fingerprint(expression, dialect),
        join_count=join_count,
    )


def apply_result_governance(encoded_result: str, policy: dict[str, Any] | None = None) -> str:
    """Apply masking and connection-specific export caps to a normalized result."""
    policy = policy or {}
    try:
        payload = json.loads(encoded_result)
    except (TypeError, ValueError):
        return encoded_result
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return encoded_result

    masks = list(policy.get("column_masks") or [])
    pii_labels = policy.get("pii_labels") or {}
    mask_labels = set(policy.get("mask_pii_labels") or [])
    masked_indexes = {
        index
        for index, column in enumerate(columns)
        if _matches(str(column), masks) or pii_labels.get(str(column)) in mask_labels
    }
    for row in rows:
        if isinstance(row, list):
            for index in masked_indexes:
                if index < len(row):
                    row[index] = "[REDACTED]"

    max_rows = max(0, int(policy.get("max_export_rows", len(rows))))
    if len(rows) > max_rows:
        del rows[max_rows:]
        payload["truncated"] = True
    payload["row_count"] = len(rows)
    max_bytes = max(256, int(policy.get("max_result_bytes", 1024 * 1024)))
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while rows and len(encoded.encode("utf-8")) > max_bytes:
        rows.pop()
        payload["row_count"] = len(rows)
        payload["truncated"] = True
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        return json.dumps(
            {
                "error": "The data source result exceeds its policy limit.",
                "truncated": True,
            }
        )
    return encoded
