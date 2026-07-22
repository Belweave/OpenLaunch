from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from openlaunch.models.config import Config

AGENTMAIL_API_BASE_URL = "https://api.agentmail.to"
AGENTMAIL_TIMEOUT = 30.0
USER_INBOXES_CONFIG_KEY = "email.agentmail.user_inboxes"

_mapping_lock = asyncio.Lock()
log = logging.getLogger(__name__)


class AgentMailError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def get_agentmail_settings() -> tuple[bool, str]:
    values = await Config.get_many("email.agentmail.enable", "email.agentmail.api_key")
    return (
        bool(values.get("email.agentmail.enable")),
        str(values.get("email.agentmail.api_key") or "").strip(),
    )


async def agentmail_request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    params: Any = None,
    json_body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if (
        not path.startswith("/v0/")
        or "://" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise AgentMailError(400, "Invalid AgentMail API path")

    if api_key is None:
        _, api_key = await get_agentmail_settings()
    if not api_key:
        raise AgentMailError(503, "AgentMail API key is not configured")

    request_headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    try:
        async with httpx.AsyncClient(
            timeout=AGENTMAIL_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.request(
                method.upper(),
                f"{AGENTMAIL_API_BASE_URL}{path}",
                params=params,
                json=json_body,
                content=content,
                headers=request_headers,
            )
    except httpx.RequestError as exc:
        log.warning("AgentMail network request failed (%s)", type(exc).__name__)
        raise AgentMailError(502, "AgentMail is temporarily unavailable") from exc

    if response.status_code >= 400:
        log.warning(
            "AgentMail request failed (status=%s path=%s)", response.status_code, path
        )
        detail = {
            400: "AgentMail rejected the request as invalid",
            401: "AgentMail rejected the configured API key",
            403: "The AgentMail API key does not permit this operation",
            404: "The requested AgentMail resource was not found",
            409: "The AgentMail request conflicts with an existing resource",
            422: "AgentMail could not validate the request",
            429: "AgentMail rate limit reached; try again shortly",
        }.get(response.status_code, "AgentMail request failed")
        raise AgentMailError(response.status_code, detail)
    return response


async def get_mapped_inbox_id(user_id: str) -> str | None:
    mappings = await Config.get(USER_INBOXES_CONFIG_KEY, {}) or {}
    return mappings.get(user_id)


async def set_mapped_inbox_id(user_id: str, inbox_id: str | None) -> None:
    async with _mapping_lock:
        mappings = dict(await Config.get(USER_INBOXES_CONFIG_KEY, {}) or {})
        if inbox_id:
            mappings[user_id] = inbox_id
        else:
            mappings.pop(user_id, None)
        await Config.upsert({USER_INBOXES_CONFIG_KEY: mappings})


async def find_user_inbox(user_id: str) -> dict | None:
    mapped_id = await get_mapped_inbox_id(user_id)
    if mapped_id:
        try:
            response = await agentmail_request(
                "GET", f'/v0/inboxes/{quote(mapped_id, safe="")}'
            )
            return response.json()
        except AgentMailError as exc:
            if exc.status_code != 404:
                raise
            await set_mapped_inbox_id(user_id, None)

    page_token = None
    while True:
        params = {"limit": 100}
        if page_token:
            params["page_token"] = page_token
        response = await agentmail_request("GET", "/v0/inboxes", params=params)
        payload = response.json()
        for inbox in payload.get("inboxes", []):
            metadata = inbox.get("metadata") or {}
            if (
                inbox.get("client_id") == f"openlaunch:{user_id}"
                or metadata.get("openlaunch_user_id") == user_id
            ):
                await set_mapped_inbox_id(user_id, inbox["inbox_id"])
                return inbox
        page_token = payload.get("next_page_token")
        if not page_token:
            return None


async def provision_user_inbox(
    user: dict,
    *,
    username: str | None = None,
    domain: str | None = None,
    display_name: str | None = None,
) -> dict:
    existing = await find_user_inbox(user["id"])
    if existing:
        return existing

    body: dict[str, Any] = {
        "client_id": f'openlaunch:{user["id"]}',
        "display_name": display_name or user.get("name") or user.get("email"),
        "metadata": {
            "openlaunch_user_id": user["id"],
            "openlaunch_user_email": user.get("email", ""),
        },
    }
    if username:
        body["username"] = username.strip()
    if domain:
        body["domain"] = domain.strip()

    response = await agentmail_request("POST", "/v0/inboxes", json_body=body)
    inbox = response.json()
    await set_mapped_inbox_id(user["id"], inbox["inbox_id"])
    return inbox


async def require_user_inbox(user_id: str) -> dict:
    enabled, _ = await get_agentmail_settings()
    if not enabled:
        raise AgentMailError(403, "AgentMail email is disabled by the administrator")
    inbox = await find_user_inbox(user_id)
    if not inbox:
        raise AgentMailError(404, "No AgentMail inbox is provisioned for this user")
    return inbox
