import json
import unittest

from openlaunch.tools.sql_policy import (
    SQLPolicyError,
    apply_result_governance,
    enforce_sql_policy,
)


class SQLPolicyTests(unittest.TestCase):
    def test_dialects_extract_objects_and_fingerprint_literals(self):
        for provider, query in (
            ("postgresql", "SELECT id FROM analytics.orders WHERE id = 7"),
            ("sql_server", "SELECT TOP 5 id FROM dbo.orders"),
            ("azure_sql", "SELECT id FROM dbo.orders"),
            ("snowflake", "SELECT ID FROM ANALYTICS.PUBLIC.ORDERS"),
        ):
            decision = enforce_sql_policy(query, provider)
            self.assertTrue(decision.objects)
            self.assertEqual(len(decision.fingerprint), 64)
        self.assertEqual(
            enforce_sql_policy("SELECT id FROM orders WHERE id = 7", "postgresql").fingerprint,
            enforce_sql_policy("SELECT id FROM orders WHERE id = 8", "postgresql").fingerprint,
        )

    def test_rejects_writable_cte_multi_statement_procedure_and_denied_object(self):
        cases = (
            (
                "postgresql",
                "WITH changed AS (DELETE FROM accounts RETURNING *) SELECT * FROM changed",
                {},
            ),
            ("postgresql", "SELECT 1; SELECT 2", {}),
            ("sql_server", "EXEC dangerous_procedure", {}),
            ("postgresql", "SELECT pg_read_file('/etc/passwd')", {}),
            (
                "snowflake",
                "SELECT * FROM private.secrets",
                {"object_allowlist": ["analytics.*"]},
            ),
        )
        for provider, query, policy in cases:
            with self.subTest(query=query), self.assertRaises(SQLPolicyError):
                enforce_sql_policy(query, provider, policy)

    def test_column_rules_row_predicate_cost_and_masking(self):
        decision = enforce_sql_policy(
            "SELECT id, email FROM analytics.orders",
            "postgresql",
            {
                "object_allowlist": ["analytics.*"],
                "row_predicates": {"analytics.orders": "tenant_id = 42"},
                "max_joins": 0,
            },
        )
        self.assertIn("tenant_id", decision.query)
        encoded = json.dumps(
            {
                "columns": ["id", "email"],
                "rows": [[1, "a@example.com"], [2, "b@example.com"]],
                "row_count": 2,
            }
        )
        governed = json.loads(apply_result_governance(encoded, {"column_masks": ["email"], "max_export_rows": 1}))
        self.assertEqual(governed["rows"], [[1, "[REDACTED]"]])
        self.assertTrue(governed["truncated"])

        with self.assertRaises(SQLPolicyError):
            enforce_sql_policy(
                "SELECT password_hash FROM analytics.users",
                "postgresql",
                {"column_denylist": ["password_hash"]},
            )
