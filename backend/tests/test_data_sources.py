import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from openlaunch.tools import data_sources


class _ContextManager:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False


class DataSourceConfigurationTests(unittest.TestCase):
    def test_legacy_postgres_connection_keeps_global_permission_as_access_gate(self):
        with (
            patch.object(data_sources, "DATA_SOURCE_CONNECTIONS", "[]"),
            patch.object(data_sources, "DATA_SOURCE_CONNECTIONS_FILE", ""),
            patch.object(data_sources, "ENABLE_SQL_DATABASE_TOOL", True),
            patch.object(
                data_sources,
                "SQL_DATABASE_URL",
                "postgresql://reader:secret@example/database",
            ),
        ):
            connection = data_sources.get_configured_data_sources()[0]

        self.assertEqual(connection["id"], "default-postgresql")
        self.assertEqual(connection["access_grants"][0]["principal_id"], "*")

    def test_loads_multiple_named_connections_and_normalizes_aliases(self):
        document = json.dumps(
            {
                "operations": {
                    "type": "mssql",
                    "connection_string": "secret",
                    "description": "Operations",
                },
                "analytics": {
                    "type": "snowflake",
                    "config": {"account": "example"},
                },
                "cache": {"type": "redis", "url": "rediss://secret"},
            }
        )
        with (
            patch.object(data_sources, "DATA_SOURCE_CONNECTIONS", document),
            patch.object(data_sources, "DATA_SOURCE_CONNECTIONS_FILE", ""),
            patch.object(data_sources, "ENABLE_SQL_DATABASE_TOOL", False),
        ):
            configured = data_sources.get_configured_data_sources()

        self.assertEqual(
            [(item["id"], item["type"]) for item in configured],
            [
                ("operations", "sql_server"),
                ("analytics", "snowflake"),
                ("cache", "redis"),
            ],
        )

    def test_list_returns_only_safe_metadata_for_authorized_connections(self):
        connections = [
            {
                "id": "warehouse",
                "type": "snowflake",
                "description": "Published analytics",
                "config": {"password": "never-return-this"},
            }
        ]

        async def run():
            with patch.object(data_sources, "get_configured_data_sources", return_value=connections):
                return await data_sources.list_data_sources({"id": "admin", "role": "admin"})

        payload = asyncio.run(run())
        self.assertEqual(
            json.loads(payload),
            {
                "data_sources": [
                    {
                        "connection_id": "warehouse",
                        "type": "snowflake",
                        "description": "Published analytics",
                    }
                ]
            },
        )
        self.assertNotIn("never-return-this", payload)

    def test_missing_and_unauthorized_connections_have_same_error(self):
        connection = {
            "id": "private",
            "type": "postgresql",
            "url": "postgresql://secret",
        }

        async def run(connection_id):
            with (
                patch.object(
                    data_sources,
                    "get_configured_data_sources",
                    return_value=[connection],
                ),
                patch.object(
                    data_sources.Groups,
                    "get_groups_by_member_id",
                    AsyncMock(return_value=[]),
                ),
            ):
                return await data_sources.query_data_source(connection_id, "SELECT 1", {"id": "user", "role": "user"})

        missing = json.loads(asyncio.run(run("missing")))
        unauthorized = json.loads(asyncio.run(run("private")))
        self.assertEqual(missing, unauthorized)
        self.assertEqual(missing["error"], "The requested data source is not available.")


class DataSourceValidationTests(unittest.TestCase):
    def test_allows_readonly_queries_and_trailing_semicolon(self):
        cases = [
            "SELECT 1;",
            "-- report\nWITH totals AS (SELECT 1) SELECT * FROM totals",
            "SELECT 'semi;colon' AS value",
            "SELECT $$semi;colon$$ AS value",
            "EXPLAIN SELECT * FROM widgets",
            "SHOW search_path",
            "DESCRIBE TABLE metrics",
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertTrue(data_sources.validate_readonly_query(query))

    def test_rejects_mutations_multiple_statements_and_unterminated_input(self):
        cases = [
            "INSERT INTO widgets(name) VALUES ('x')",
            "UPDATE widgets SET name = 'x'",
            "DELETE FROM widgets",
            "SELECT 1; SELECT 2",
            "SELECT 'unterminated",
            "/* unterminated SELECT 1",
        ]
        for query in cases:
            with self.subTest(query=query):
                with self.assertRaises(data_sources.DataSourceValidationError):
                    data_sources.validate_readonly_query(query)

    def test_normalizes_supported_postgres_urls(self):
        self.assertEqual(
            data_sources._postgres_engine_url("postgres://user:pass@db.example/app"),
            "postgresql+psycopg://user:pass@db.example/app",
        )
        with self.assertRaises(ValueError):
            data_sources._postgres_engine_url("mysql://db.example/app")


class DataSourceAdapterTests(unittest.TestCase):
    def test_postgres_uses_readonly_transaction_and_caps_rows(self):
        result = MagicMock()
        result.returns_rows = True
        result.keys.return_value = ["id", "created_at"]
        result.fetchmany.return_value = [
            (1, datetime(2026, 7, 21, tzinfo=timezone.utc)),
            (2, datetime(2026, 7, 22, tzinfo=timezone.utc)),
        ]
        query_connection = MagicMock()
        query_connection.execute.return_value = result
        connection = MagicMock()
        connection.begin.return_value = _ContextManager(None)
        connection.execution_options.return_value = query_connection
        engine = MagicMock()
        engine.connect.return_value = _ContextManager(connection)

        with (
            patch.object(data_sources, "_get_postgres_engine", return_value=engine),
            patch.object(data_sources, "DATA_SOURCE_MAX_ROWS", 1),
        ):
            payload = json.loads(
                data_sources._execute_postgresql({"id": "postgres", "type": "postgresql"}, "SELECT * FROM events")
            )

        self.assertEqual(payload["rows"], [[1, "2026-07-21T00:00:00+00:00"]])
        self.assertTrue(payload["truncated"])
        setup = [str(call.args[0]) for call in connection.execute.call_args_list]
        self.assertEqual(setup[0], "SET TRANSACTION READ ONLY")

    def test_sql_server_uses_readonly_odbc_connection_and_rolls_back(self):
        cursor = MagicMock()
        cursor.description = [("id",)]
        cursor.fetchmany.return_value = [(1,)]
        database = MagicMock()
        database.cursor.return_value = cursor
        pyodbc = types.ModuleType("pyodbc")
        pyodbc.connect = MagicMock(return_value=database)
        connection = {
            "id": "operations",
            "type": "azure_sql",
            "connection_string": "Driver={ODBC Driver 18 for SQL Server};Server=example",
        }

        with patch.dict(sys.modules, {"pyodbc": pyodbc}):
            payload = json.loads(data_sources._execute_mssql(connection, "SELECT id FROM customers"))

        self.assertEqual(payload["rows"], [[1]])
        self.assertTrue(pyodbc.connect.call_args.kwargs["readonly"])
        database.rollback.assert_called_once()
        database.close.assert_called_once()

    def test_snowflake_sets_limits_tag_and_rolls_back(self):
        cursor = MagicMock()
        cursor.description = [("TOTAL",)]
        cursor.fetchmany.return_value = [(42,)]
        database = MagicMock()
        database.__enter__.return_value = database
        database.__exit__.return_value = False
        database.cursor.return_value = cursor
        connector = types.ModuleType("snowflake.connector")
        connector.connect = MagicMock(return_value=database)
        snowflake = types.ModuleType("snowflake")
        snowflake.connector = connector
        connection = {
            "id": "warehouse",
            "type": "snowflake",
            "config": {"account": "example", "user": "reader"},
        }

        with patch.dict(
            sys.modules,
            {"snowflake": snowflake, "snowflake.connector": connector},
        ):
            payload = json.loads(data_sources._execute_snowflake(connection, "SELECT COUNT(*) FROM metrics"))

        self.assertEqual(payload["rows"], [[42]])
        config = connector.connect.call_args.kwargs
        self.assertEqual(config["application"], "OpenLaunch")
        self.assertEqual(config["session_parameters"]["QUERY_TAG"], "openlaunch_data_source_tool")
        database.rollback.assert_called_once()

    def test_redis_exposes_bounded_read_operations(self):
        client = MagicMock()
        client.hscan_iter.return_value = iter((("one", "1"), ("two", "2")))
        redis_class = MagicMock()
        redis_class.from_url.return_value = client
        redis_module = types.ModuleType("redis")
        redis_module.Redis = redis_class
        connection = {
            "id": "cache",
            "type": "redis",
            "url": "rediss://reader@example/0",
        }

        with (
            patch.dict(sys.modules, {"redis": redis_module}),
            patch.object(data_sources, "DATA_SOURCE_MAX_ROWS", 1),
        ):
            payload = json.loads(
                data_sources._execute_redis_read(connection, "hgetall", "record:1", None, None, 0, None)
            )

        self.assertEqual(payload["rows"], [["one", "1"]])
        self.assertTrue(payload["truncated"])
        client.close.assert_called_once()

    def test_query_returns_customer_safe_errors(self):
        connection = {"id": "warehouse", "type": "snowflake"}

        async def run():
            with (
                patch.object(
                    data_sources,
                    "_get_authorized_data_source",
                    AsyncMock(return_value=connection),
                ),
                patch.object(
                    data_sources,
                    "_execute_sql",
                    side_effect=RuntimeError("password=secret private-host"),
                ),
            ):
                return await data_sources.query_data_source("warehouse", "SELECT 1")

        payload = asyncio.run(run())
        self.assertEqual(json.loads(payload), {"error": "The data source query failed."})
        self.assertNotIn("secret", payload)

    def test_snowflake_schema_inspection_uses_connector_placeholders(self):
        connection = {"id": "warehouse", "type": "snowflake"}
        execute = MagicMock(return_value='{"rows":[]}')

        async def run():
            with (
                patch.object(
                    data_sources,
                    "_get_authorized_data_source",
                    AsyncMock(return_value=connection),
                ),
                patch.object(data_sources, "_execute_sql", execute),
            ):
                return await data_sources.inspect_data_source("warehouse", namespace="PUBLISHED")

        asyncio.run(run())
        query = execute.call_args.args[1]
        self.assertIn("%(namespace)s", query)
        self.assertNotIn(":namespace", query)


class DataSourceRegistryTests(unittest.TestCase):
    def test_registry_injects_all_data_source_tools_for_authorized_user(self):
        from openlaunch.utils import tools as tool_utils

        disabled = {
            name: False
            for name in (
                "time",
                "knowledge",
                "chats",
                "memory",
                "web_search",
                "image_generation",
                "code_interpreter",
                "notes",
                "channels",
                "tasks",
                "automations",
                "calendar",
            )
        }
        model = {"info": {"meta": {"builtinTools": {**disabled, "data_sources": True}}}}

        async def run():
            with (
                patch.object(tool_utils, "is_data_sources_configured", return_value=True),
                patch.object(tool_utils.Config, "get_many", AsyncMock(return_value={})),
            ):
                return await tool_utils.get_builtin_tools(
                    MagicMock(),
                    {"__user__": {"id": "admin", "role": "admin"}},
                    features={},
                    model=model,
                )

        tools = asyncio.run(run())
        self.assertEqual(
            set(tools),
            {
                "list_data_sources",
                "inspect_data_source",
                "query_data_source",
                "read_redis_data_source",
            },
        )
        self.assertEqual(tools["query_data_source"]["tool_id"], "builtin:query_data_source")


if __name__ == "__main__":
    unittest.main()
