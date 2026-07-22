import unittest
import uuid

from pydantic import ValidationError

from openlaunch.models.control_plane import (
    ControlPlanes,
    DataConnectionForm,
    ToolProfileForm,
)


class ControlPlaneValidationTests(unittest.TestCase):
    def test_connection_metadata_and_secret_reference_are_separated(self):
        with self.assertRaises(ValidationError):
            DataConnectionForm(
                id="unsafe",
                provider_type="postgresql",
                safe_metadata={"nested": [{"password": "do-not-store"}]},
            )
        with self.assertRaises(ValidationError):
            DataConnectionForm(
                id="unsafe-ref",
                provider_type="postgresql",
                secret_ref={"type": "env", "field": "url"},
            )
        with self.assertRaises(ValidationError):
            DataConnectionForm(
                id="literal-in-disguise",
                provider_type="postgresql",
                secret_ref={
                    "type": "env",
                    "name": "ANALYTICS_DATABASE_URL",
                    "field": "url",
                    "value": "do-not-store",
                },
            )

        form = DataConnectionForm(
            id="safe",
            provider_type="postgresql",
            safe_metadata={"host": "database.internal", "database": "analytics"},
            secret_ref={"type": "env", "name": "ANALYTICS_DATABASE_URL", "field": "url"},
        )
        self.assertEqual(form.secret_ref["name"], "ANALYTICS_DATABASE_URL")

    def test_profile_bundle_types_are_fail_closed(self):
        with self.assertRaises(ValidationError):
            ToolProfileForm(
                id="invalid",
                name="Invalid",
                assignments=[{"scope_type": "instance", "scope_id": "*"}],
                bundle={"tool_ids": "tool-a"},
            )


class ToolProfileAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_profile_requires_an_assigned_principal(self):
        profile_id = f"test-{uuid.uuid4()}"
        form = ToolProfileForm(
            id=profile_id,
            name="Credential-bound test",
            assignments=[{"scope_type": "api_credential", "scope_id": "credential-a"}],
            bundle={"tool_ids": ["tool-a"], "data_source_grants": ["warehouse"]},
        )
        try:
            await ControlPlanes.upsert_profile(form, "test-admin")
            denied = await ControlPlanes.resolve_profile(
                profile_id,
                user_id="user-a",
                model_id="model-a",
                scopes={"api_credential": "credential-b"},
            )
            allowed = await ControlPlanes.resolve_profile(
                profile_id,
                user_id="user-a",
                model_id="model-a",
                scopes={"api_credential": "credential-a"},
            )
            self.assertIsNone(denied)
            self.assertEqual(allowed["tool_ids"], ["tool-a"])
            self.assertEqual(allowed["data_source_grants"], ["warehouse"])
        finally:
            await ControlPlanes.delete_profile(profile_id)


if __name__ == "__main__":
    unittest.main()
