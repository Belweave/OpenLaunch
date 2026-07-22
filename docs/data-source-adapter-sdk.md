# Data-source adapter SDK

OpenLaunch data adapters expose a provider-neutral, bounded read surface. Extensions register a `DataSourceAdapter` from `openlaunch.tools.data_source_sdk`; the core registry does not need editing.

## Contract

An adapter declares one or more unique `provider_types` and immutable `AdapterCapabilities`. It implements `test_connection(connection)` and, when supported, `inspect(...)` and `query(connection, operation, **options)`. Registration fails on provider-name collisions. Capability discovery is available through `adapter_capabilities()` and the admin control-plane API.

Connection records keep safe routing metadata apart from `secret_ref`. Core resolvers support `{ "type": "env", "name": "...", "field": "url" }` and `{ "type": "file", "path": "/absolute/path", "key": "optional-json-key", "field": "password" }`. `register_secret_resolver` supports Vault, Kubernetes, AWS, Azure, or other backends without changing adapter code. Resolvers must return only the secret value, never log it, and raise `SecretResolutionError` with no upstream details.

## Safety requirements

Adapters must:

- return only safe connection identifiers, provider types, normalized columns/rows, counts, and truncation state;
- implement bounded operations rather than arbitrary command pass-through;
- honor application deadlines, cancellation, row/result byte limits, and database-driver timeouts;
- validate operations before I/O and use least-privilege, read-only upstream credentials;
- roll back SQL transactions and reject statements that cannot be classified;
- raise internal errors without embedding credentials, URLs, driver details, or upstream text in model-visible output;
- integrate the connection policy, tool annotations, and append-only audit hook.

The built-in `SQLDataSourceAdapter` proves PostgreSQL, T-SQL/Azure SQL, and Snowflake behind this interface. `RedisDataSourceAdapter` proves a non-SQL bounded-operation adapter; it intentionally exposes only scan/type/TTL and capped value/range reads.

## SQL policy and governance

`enforce_sql_policy` uses SQLGlot ASTs for the configured dialect. It accepts one query, rejects mutations (including writable CTEs), procedure/command nodes and unsafe functions, extracts object and column references, and enforces object/column allow/deny lists plus join/scan safeguards. Configure connection `policy` with fields such as:

```json
{
  "object_allowlist": ["analytics.*"],
  "column_denylist": ["*.password_hash"],
  "row_predicates": {"analytics.orders": "tenant_id = current_setting('app.tenant_id')"},
  "column_masks": ["email", "phone"],
  "pii_labels": {"customer_email": "contact"},
  "mask_pii_labels": ["contact"],
  "max_joins": 6,
  "max_scans": 10,
  "max_export_rows": 500,
  "max_result_bytes": 131072,
  "audit_raw_sql": false
}
```

Application parsing is defense in depth. Deployments must still use dedicated read-only accounts and must never reuse OpenLaunch's application `DATABASE_URL`.

## Audit and conformance

Each SQL query appends safe metadata: actor, connection/provider, request/tool correlation IDs, referenced objects, policy decision, timestamps/duration, row/result sizes, status, and a literal-normalized fingerprint. Raw SQL is absent unless an administrator explicitly enables it. Audit records have no update API; the only deletion route is the admin retention operation.

Adapter tests must cover success, malformed operations, deadlines/cancellation, size truncation, policy denial, secret and exception sanitization, and representative upstream failures. Run the full backend suite and a production frontend build before shipping an adapter.

## Compatibility

Persisted connections are merged with `DATA_SOURCE_CONNECTIONS`, `DATA_SOURCE_CONNECTIONS_FILE`, and legacy `SQL_DATABASE_URL`. Duplicate IDs fail closed to the first configured entry. This permits gradual migration: create and test a persisted record with a new ID, move grants, switch callers, then remove the environment entry.
