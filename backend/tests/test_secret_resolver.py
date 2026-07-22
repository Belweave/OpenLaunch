import os
import tempfile
import unittest
from unittest.mock import patch

from openlaunch.utils.secret_resolver import (
    SecretResolutionError,
    inject_connection_secret,
    resolve_secret,
)


class SecretResolverTests(unittest.TestCase):
    def test_environment_and_file_references(self):
        with patch.dict(os.environ, {"OPENLAUNCH_TEST_SECRET": "secret-value"}):
            self.assertEqual(
                resolve_secret({"type": "env", "name": "OPENLAUNCH_TEST_SECRET"}),
                "secret-value",
            )
        with tempfile.NamedTemporaryFile(mode="w+", delete=True) as handle:
            handle.write('{"url":"redis://secret-host"}')
            handle.flush()
            result = inject_connection_secret(
                {"database": "cache"},
                {"type": "file", "path": handle.name, "key": "url", "field": "url"},
            )
            self.assertEqual(result["url"], "redis://secret-host")

    def test_missing_secret_error_is_generic(self):
        with self.assertRaisesRegex(SecretResolutionError, "^Secret is unavailable\\.$"):
            resolve_secret({"type": "env", "name": "CERTAINLY_MISSING_OPENLAUNCH_SECRET"})
