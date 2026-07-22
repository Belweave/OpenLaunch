from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from openlaunch.models.config import Config
from openlaunch.utils.agentmail import (
    AgentMailError,
    agentmail_request,
    create_additional_user_inbox,
    delete_user_inbox,
    find_user_inbox,
    get_agentmail_settings,
    list_agentmail_domains,
    list_user_inboxes,
    provision_user_inbox,
    require_user_inbox,
    select_user_inbox,
)
from openlaunch.utils.auth import get_admin_user, get_verified_user
from pydantic import BaseModel

router = APIRouter()


def _raise_agentmail_error(exc: AgentMailError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _response_from_agentmail(response) -> Response:
    content_type = response.headers.get("content-type", "application/json")
    headers = {}
    for name in ("content-disposition", "retry-after"):
        if response.headers.get(name):
            headers[name] = response.headers[name]
    if "application/json" in content_type:
        return JSONResponse(
            response.json(), status_code=response.status_code, headers=headers
        )
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=content_type.split(";", 1)[0],
        headers=headers,
    )


class AgentMailAdminConfig(BaseModel):
    ENABLE_AGENTMAIL: bool
    AGENTMAIL_API_KEY: str | None = None
    CLEAR_AGENTMAIL_API_KEY: bool = False


class ProvisionInboxForm(BaseModel):
    username: str | None = None
    domain: str | None = None
    display_name: str | None = None


class SelectInboxForm(BaseModel):
    inbox_id: str


@router.get("/admin/config")
async def get_admin_agentmail_config(user=Depends(get_admin_user)):
    enabled, api_key = await get_agentmail_settings()
    return {
        "ENABLE_AGENTMAIL": enabled,
        "AGENTMAIL_API_KEY": "",
        "HAS_AGENTMAIL_API_KEY": bool(api_key),
    }


@router.post("/admin/config")
async def set_admin_agentmail_config(
    form_data: AgentMailAdminConfig, user=Depends(get_admin_user)
):
    _, existing_api_key = await get_agentmail_settings()
    api_key = (
        ""
        if form_data.CLEAR_AGENTMAIL_API_KEY
        else (form_data.AGENTMAIL_API_KEY or "").strip() or existing_api_key
    )
    if form_data.ENABLE_AGENTMAIL and not api_key:
        raise HTTPException(
            status_code=400,
            detail="An AgentMail API key is required when email is enabled",
        )

    if form_data.AGENTMAIL_API_KEY and api_key:
        try:
            await agentmail_request("GET", "/v0/auth/me", api_key=api_key)
        except AgentMailError as exc:
            _raise_agentmail_error(exc)

    await Config.upsert(
        {
            "email.agentmail.enable": form_data.ENABLE_AGENTMAIL,
            "email.agentmail.api_key": api_key,
        }
    )
    return {
        "ENABLE_AGENTMAIL": form_data.ENABLE_AGENTMAIL,
        "AGENTMAIL_API_KEY": "",
        "HAS_AGENTMAIL_API_KEY": bool(api_key),
    }


@router.post("/admin/test")
async def test_admin_agentmail_config(user=Depends(get_admin_user)):
    try:
        response = await agentmail_request("GET", "/v0/auth/me")
        return {"status": True, "account": response.json()}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.get("/me/inbox")
async def get_my_agentmail_inbox(user=Depends(get_verified_user)):
    enabled, api_key = await get_agentmail_settings()
    if not enabled:
        return {"enabled": False, "configured": bool(api_key), "inbox": None}
    try:
        inbox = await find_user_inbox(user.id)
        return {"enabled": True, "configured": bool(api_key), "inbox": inbox}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.post("/me/inbox")
async def provision_my_agentmail_inbox(
    form_data: ProvisionInboxForm, user=Depends(get_verified_user)
):
    enabled, _ = await get_agentmail_settings()
    if not enabled:
        raise HTTPException(
            status_code=403, detail="AgentMail email is disabled by the administrator"
        )
    try:
        inbox = await provision_user_inbox(
            user.model_dump(),
            username=form_data.username,
            domain=form_data.domain,
            display_name=form_data.display_name,
        )
        return {"inbox": inbox}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.get("/me/inboxes")
async def get_my_agentmail_inboxes(user=Depends(get_verified_user)):
    enabled, api_key = await get_agentmail_settings()
    if not enabled:
        return {
            "enabled": False,
            "configured": bool(api_key),
            "inbox": None,
            "inboxes": [],
        }
    try:
        active_inbox = await find_user_inbox(user.id)
        return {
            "enabled": True,
            "configured": bool(api_key),
            "inbox": active_inbox,
            "inboxes": await list_user_inboxes(user.id),
        }
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.post("/me/inboxes")
async def create_my_additional_agentmail_inbox(
    form_data: ProvisionInboxForm, user=Depends(get_verified_user)
):
    enabled, _ = await get_agentmail_settings()
    if not enabled:
        raise HTTPException(
            status_code=403, detail="AgentMail email is disabled by the administrator"
        )
    try:
        inbox = await create_additional_user_inbox(
            user.model_dump(),
            username=form_data.username,
            domain=form_data.domain,
            display_name=form_data.display_name,
        )
        return {"inbox": inbox}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.post("/me/inboxes/select")
async def select_my_agentmail_inbox(
    form_data: SelectInboxForm, user=Depends(get_verified_user)
):
    try:
        return {"inbox": await select_user_inbox(user.id, form_data.inbox_id)}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.delete("/me/inboxes/{inbox_id}")
async def delete_my_agentmail_inbox(inbox_id: str, user=Depends(get_verified_user)):
    try:
        next_inbox = await delete_user_inbox(user.id, inbox_id)
        return {"deleted": True, "inbox": next_inbox}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.get("/me/domains")
async def get_my_agentmail_domains(user=Depends(get_verified_user)):
    enabled, _ = await get_agentmail_settings()
    if not enabled:
        raise HTTPException(
            status_code=403, detail="AgentMail email is disabled by the administrator"
        )
    try:
        return {"domains": await list_agentmail_domains()}
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.api_route(
    "/me/client/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy_my_agentmail_client(
    path: str, request: Request, user=Depends(get_verified_user)
):
    try:
        if "://" in path or any(segment in {".", ".."} for segment in path.split("/")):
            raise AgentMailError(400, "Invalid inbox-relative AgentMail path")
        inbox = await require_user_inbox(user.id)
        inbox_id = quote(inbox["inbox_id"], safe="")
        suffix = path.strip("/")
        api_path = f"/v0/inboxes/{inbox_id}" + (f"/{suffix}" if suffix else "")

        body = await request.body()
        json_body = None
        content = None
        headers = None
        if body:
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    json_body = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise AgentMailError(
                        400, "Request body must be valid JSON"
                    ) from exc
            else:
                content = body
                headers = {
                    "Content-Type": request.headers.get(
                        "content-type", "application/octet-stream"
                    )
                }

        response = await agentmail_request(
            request.method,
            api_path,
            params=request.query_params.multi_items(),
            json_body=json_body,
            content=content,
            headers=headers,
        )
        return _response_from_agentmail(response)
    except AgentMailError as exc:
        _raise_agentmail_error(exc)


@router.api_route(
    "/admin/client/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy_admin_agentmail_client(
    path: str, request: Request, user=Depends(get_admin_user)
):
    try:
        if "://" in path or any(segment in {".", ".."} for segment in path.split("/")):
            raise AgentMailError(400, "Invalid AgentMail API path")
        body = await request.body()
        try:
            json_body = (
                json.loads(body)
                if body
                and "application/json" in request.headers.get("content-type", "")
                else None
            )
        except json.JSONDecodeError as exc:
            raise AgentMailError(400, "Request body must be valid JSON") from exc
        content = body if body and json_body is None else None
        response = await agentmail_request(
            request.method,
            f'/v0/{path.strip("/")}',
            params=request.query_params.multi_items(),
            json_body=json_body,
            content=content,
            headers=(
                {
                    "Content-Type": request.headers.get(
                        "content-type", "application/octet-stream"
                    )
                }
                if content
                else None
            ),
        )
        return _response_from_agentmail(response)
    except AgentMailError as exc:
        _raise_agentmail_error(exc)
