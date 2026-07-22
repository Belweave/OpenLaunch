import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from openlaunch.tools.agentmail import agentmail_client
from openlaunch.utils.agentmail import (
    AgentMailError,
    agentmail_request,
    delete_user_inbox,
    find_user_inbox,
    get_additional_user_client_id,
    get_user_client_id,
    list_agentmail_domains,
    list_user_inboxes,
    provision_user_inbox,
    select_user_inbox,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class AgentMailClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_agentmail_api_paths_before_network_request(self):
        with self.assertRaises(AgentMailError) as context:
            await agentmail_request(
                "GET", "https://example.com/v0/inboxes", api_key="secret"
            )
        self.assertEqual(context.exception.status_code, 400)

    async def test_sanitizes_network_errors(self):
        secret_detail = "connection failed while using am_super_secret"
        with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as request:
            request.side_effect = httpx.ConnectError(secret_detail)
            with self.assertRaises(AgentMailError) as context:
                await agentmail_request("GET", "/v0/auth/me", api_key="am_super_secret")

        self.assertEqual(context.exception.status_code, 502)
        self.assertNotIn("am_super_secret", context.exception.detail)
        self.assertNotIn("connection failed", context.exception.detail)

    async def test_sanitizes_upstream_error_bodies(self):
        response = httpx.Response(
            401,
            json={"message": "invalid am_super_secret for tenant internal-123"},
            request=httpx.Request("GET", "https://api.agentmail.to/v0/auth/me"),
        )
        with patch(
            "httpx.AsyncClient.request", new_callable=AsyncMock, return_value=response
        ):
            with self.assertRaises(AgentMailError) as context:
                await agentmail_request("GET", "/v0/auth/me", api_key="am_super_secret")

        self.assertEqual(context.exception.status_code, 401)
        self.assertNotIn("am_super_secret", context.exception.detail)
        self.assertNotIn("internal-123", context.exception.detail)

    @patch("openlaunch.utils.agentmail.set_mapped_inbox_id", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.agentmail_request", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.get_mapped_inbox_id", new_callable=AsyncMock)
    async def test_finds_and_links_existing_openlaunch_inbox(
        self, get_mapping, request, set_mapping
    ):
        get_mapping.return_value = None
        request.return_value = FakeResponse(
            {
                "inboxes": [
                    {
                        "inbox_id": "admin@agentmail.to",
                        "client_id": "openlaunch:user-1",
                        "metadata": {"openlaunch_user_id": "user-1"},
                    }
                ]
            }
        )

        inbox = await find_user_inbox("user-1")

        self.assertEqual(inbox["inbox_id"], "admin@agentmail.to")
        set_mapping.assert_awaited_once_with("user-1", "admin@agentmail.to")

    @patch("openlaunch.utils.agentmail.set_mapped_inbox_id", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.agentmail_request", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.find_user_inbox", new_callable=AsyncMock)
    async def test_provisions_idempotent_user_inbox(
        self, find_inbox, request, set_mapping
    ):
        find_inbox.return_value = None
        request.return_value = FakeResponse({"inbox_id": "preetham@agentmail.to"})

        inbox = await provision_user_inbox(
            {"id": "user-1", "name": "Preetham", "email": "preetham@example.com"},
            username="preetham",
        )

        self.assertEqual(inbox["inbox_id"], "preetham@agentmail.to")
        body = request.await_args.kwargs["json_body"]
        self.assertEqual(body["client_id"], "openlaunch-user-1")
        self.assertEqual(body["metadata"]["openlaunch_user_id"], "user-1")
        set_mapping.assert_awaited_once_with("user-1", "preetham@agentmail.to")

    def test_generated_client_id_uses_only_agentmail_safe_characters(self):
        self.assertEqual(get_user_client_id("user-1_ABC"), "openlaunch-user-1_ABC")
        self.assertNotIn(":", get_user_client_id("user-1"))

    def test_additional_client_id_is_stable_for_requested_address(self):
        first = get_additional_user_client_id(
            "user-1", username="Support", domain="example.com"
        )
        second = get_additional_user_client_id(
            "user-1", username="support", domain="example.com"
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("openlaunch-user-1-"))

    @patch("openlaunch.utils.agentmail.agentmail_request", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.get_mapped_inbox_id", new_callable=AsyncMock)
    async def test_lists_only_inboxes_linked_to_user(self, get_mapping, request):
        get_mapping.return_value = "mapped@example.com"
        request.return_value = FakeResponse(
            {
                "inboxes": [
                    {"inbox_id": "mapped@example.com"},
                    {
                        "inbox_id": "owned@example.com",
                        "metadata": {"openlaunch_user_id": "user-1"},
                    },
                    {
                        "inbox_id": "foreign@example.com",
                        "metadata": {"openlaunch_user_id": "user-2"},
                    },
                ]
            }
        )

        inboxes = await list_user_inboxes("user-1")

        self.assertEqual(
            [inbox["inbox_id"] for inbox in inboxes],
            ["mapped@example.com", "owned@example.com"],
        )

    @patch("openlaunch.utils.agentmail.set_mapped_inbox_id", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.list_user_inboxes", new_callable=AsyncMock)
    async def test_selects_only_linked_inbox(self, list_inboxes, set_mapping):
        list_inboxes.return_value = [{"inbox_id": "owned@example.com"}]

        inbox = await select_user_inbox("user-1", "owned@example.com")

        self.assertEqual(inbox["inbox_id"], "owned@example.com")
        set_mapping.assert_awaited_once_with("user-1", "owned@example.com")

        with self.assertRaises(AgentMailError):
            await select_user_inbox("user-1", "foreign@example.com")

    @patch("openlaunch.utils.agentmail.set_mapped_inbox_id", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.get_mapped_inbox_id", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.agentmail_request", new_callable=AsyncMock)
    @patch("openlaunch.utils.agentmail.list_user_inboxes", new_callable=AsyncMock)
    async def test_delete_switches_to_remaining_inbox(
        self, list_inboxes, request, get_mapping, set_mapping
    ):
        get_mapping.return_value = "first@example.com"
        list_inboxes.return_value = [
            {"inbox_id": "first@example.com"},
            {"inbox_id": "second@example.com"},
        ]

        next_inbox = await delete_user_inbox("user-1", "first@example.com")

        self.assertEqual(next_inbox["inbox_id"], "second@example.com")
        request.assert_awaited_once_with("DELETE", "/v0/inboxes/first%40example.com")
        set_mapping.assert_awaited_once_with("user-1", "second@example.com")

    @patch("openlaunch.utils.agentmail.agentmail_request", new_callable=AsyncMock)
    async def test_lists_default_and_account_domains(self, request):
        request.return_value = FakeResponse(
            {
                "domains": [
                    {"domain": "agents.example.com", "domain_id": "domain-1"},
                    {"domain": "example.org", "domain_id": "domain-2"},
                ]
            }
        )

        domains = await list_agentmail_domains()

        self.assertEqual(
            [item["domain"] for item in domains],
            ["agentmail.to", "agents.example.com", "example.org"],
        )

    async def test_model_catch_all_client_rejects_path_traversal(self):
        result = json.loads(
            await agentmail_client(
                method="GET",
                path="../organizations",
                __request__=SimpleNamespace(),
                __user__={"id": "user-1"},
            )
        )
        self.assertIn("relative", result["error"])
