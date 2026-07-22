"""Backward-compatible imports for the original PostgreSQL-only tool module."""

from openlaunch.tools.data_sources import (  # noqa: F401
    DataSourceValidationError as SQLToolValidationError,
    _postgres_engine_url,
    inspect_data_source,
    is_data_sources_configured as is_sql_database_configured,
    query_data_source,
    validate_readonly_query,
)


async def query_sql_database(query: str, __user__: dict | None = None) -> str:
    """Run a legacy single-PostgreSQL query through the generic data source layer."""
    return await query_data_source("default-postgresql", query, __user__)


async def list_sql_database_schema(
    schema_name: str | None = None,
    table_name: str | None = None,
    __user__: dict | None = None,
) -> str:
    """Inspect the legacy single-PostgreSQL connection through the generic layer."""
    return await inspect_data_source(
        "default-postgresql",
        namespace=schema_name,
        object_name=table_name,
        __user__=__user__,
    )
