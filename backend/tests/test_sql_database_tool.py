import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from openlaunch.tools import sql_database


class _ContextManager:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False


class SQLDatabaseValidationTests(unittest.TestCase):
    def test_allows_readonly_queries_and_trailing_semicolon(self):
        cases = [
            "SELECT 1;",
            "-- report\nWITH totals AS (SELECT 1) SELECT * FROM totals",
            "SELECT 'semi;colon' AS value",
            "SELECT $$semi;colon$$ AS value",
            "EXPLAIN SELECT * FROM widgets",
            "SHOW search_path",
        ]
        for query in cases:
            with self.subTest(query=query):
                self.assertTrue(sql_database.validate_readonly_query(query))

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
                with self.assertRaises(sql_database.SQLToolValidationError):
                    sql_database.validate_readonly_query(query)

    def test_normalizes_supported_postgres_urls(self):
        self.assertEqual(
            sql_database._postgres_engine_url("postgres://user:pass@db.example/app"),
            "postgresql+psycopg://user:pass@db.example/app",
        )
        self.assertEqual(
            sql_database._postgres_engine_url("postgresql://user:pass@db.example/app"),
            "postgresql+psycopg://user:pass@db.example/app",
        )
        with self.assertRaises(ValueError):
            sql_database._postgres_engine_url("mysql://db.example/app")


class SQLDatabaseExecutionTests(unittest.TestCase):
    def test_executes_in_readonly_transaction_and_caps_rows(self):
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
            patch.object(sql_database, "_get_engine", return_value=engine),
            patch.object(sql_database, "SQL_DATABASE_MAX_ROWS", 1),
        ):
            payload = json.loads(
                sql_database._execute_readonly("SELECT id, created_at FROM events")
            )

        self.assertEqual(payload["columns"], ["id", "created_at"])
        self.assertEqual(payload["rows"], [[1, "2026-07-21T00:00:00+00:00"]])
        self.assertEqual(payload["row_count"], 1)
        self.assertTrue(payload["truncated"])
        setup_statements = [
            str(call.args[0]) for call in connection.execute.call_args_list
        ]
        self.assertEqual(setup_statements[0], "SET TRANSACTION READ ONLY")
        self.assertTrue(
            setup_statements[1].startswith("SET LOCAL statement_timeout = ")
        )

    def test_query_returns_generic_database_errors(self):
        async def run():
            with (
                patch.object(
                    sql_database, "is_sql_database_configured", return_value=True
                ),
                patch.object(
                    sql_database,
                    "_execute_readonly",
                    side_effect=RuntimeError(
                        "postgres://secret:password@private-host/database"
                    ),
                ),
            ):
                return await sql_database.query_sql_database("SELECT 1")

        payload = json.loads(asyncio.run(run()))
        self.assertEqual(payload, {"error": "The database query failed."})
        self.assertNotIn("secret", json.dumps(payload))

    def test_query_reports_validation_errors_without_calling_database(self):
        executor = MagicMock()

        async def run():
            with (
                patch.object(
                    sql_database, "is_sql_database_configured", return_value=True
                ),
                patch.object(sql_database, "_execute_readonly", executor),
            ):
                return await sql_database.query_sql_database("DROP TABLE customers")

        payload = json.loads(asyncio.run(run()))
        self.assertIn("Only read-only", payload["error"])
        executor.assert_not_called()


class SQLDatabaseRegistryTests(unittest.TestCase):
    def test_builtin_registry_injects_sql_tools_for_authorized_user(self):
        from openlaunch.utils import tools as tool_utils

        disabled_categories = {
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
        model = {
            "info": {
                "meta": {"builtinTools": {**disabled_categories, "sql_database": True}}
            }
        }
        request = MagicMock()

        async def run():
            with (
                patch.object(
                    tool_utils, "is_sql_database_configured", return_value=True
                ),
                patch.object(tool_utils.Config, "get_many", AsyncMock(return_value={})),
            ):
                return await tool_utils.get_builtin_tools(
                    request,
                    {"__user__": {"id": "admin", "role": "admin"}},
                    features={},
                    model=model,
                )

        tools = asyncio.run(run())
        self.assertEqual(set(tools), {"list_sql_database_schema", "query_sql_database"})
        self.assertEqual(
            tools["query_sql_database"]["tool_id"], "builtin:query_sql_database"
        )
        self.assertEqual(
            tools["query_sql_database"]["spec"]["parameters"]["required"], ["query"]
        )


if __name__ == "__main__":
    unittest.main()
