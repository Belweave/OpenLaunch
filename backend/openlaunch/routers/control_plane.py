"""Administrative APIs for generic tools and data-source control planes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from openlaunch.models.control_plane import (
    ControlPlanes,
    DataConnectionForm,
    DataConnectionResponse,
    ToolProfileForm,
    ToolProfileResponse,
    QueryAuditResponse,
    ToolProfileAuditResponse,
)
from openlaunch.tools import data_sources as _registered_adapters  # noqa: F401
from openlaunch.tools.data_source_sdk import adapter_capabilities, get_adapter
from openlaunch.utils.auth import get_admin_user
from openlaunch.utils.secret_resolver import inject_connection_secret

router = APIRouter()


@router.get("/data-connections", response_model=list[DataConnectionResponse])
async def list_data_connections(user=Depends(get_admin_user)):
    return await ControlPlanes.list_connections()


@router.put("/data-connections/{connection_id}", response_model=DataConnectionResponse)
async def upsert_data_connection(connection_id: str, form: DataConnectionForm, user=Depends(get_admin_user)):
    if connection_id != form.id:
        raise HTTPException(400, "Connection ID does not match the request path.")
    return await ControlPlanes.upsert_connection(form, user.id)


@router.post("/data-connections/{connection_id}/disable", response_model=DataConnectionResponse)
async def disable_data_connection(connection_id: str, user=Depends(get_admin_user)):
    record = await ControlPlanes.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "Connection not found.")
    form = DataConnectionForm(
        id=record.id,
        scope_type=record.scope_type,
        scope_id=record.scope_id,
        provider_type=record.provider_type,
        description=record.description,
        enabled=False,
        safe_metadata=record.safe_metadata or {},
        secret_ref=record.secret_ref,
        policy=record.policy or {},
        access_grants=record.access_grants or [],
    )
    return await ControlPlanes.upsert_connection(form, user.id)


@router.delete("/data-connections/{connection_id}")
async def delete_data_connection(connection_id: str, user=Depends(get_admin_user)):
    if not await ControlPlanes.delete_connection(connection_id):
        raise HTTPException(404, "Connection not found.")
    return {"status": "deleted"}


@router.post("/data-connections/{connection_id}/test")
async def test_data_connection(connection_id: str, user=Depends(get_admin_user)):
    record = await ControlPlanes.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "Connection not found.")
    try:
        connection = {
            **inject_connection_secret(record.safe_metadata or {}, record.secret_ref),
            "id": record.id,
            "type": record.provider_type,
        }
        await asyncio.wait_for(
            asyncio.to_thread(get_adapter(record.provider_type).test_connection, connection),
            timeout=15,
        )
        return {"status": "ok"}
    except Exception:
        # Deliberately hide credentials, drivers, hosts, and upstream text.
        raise HTTPException(400, "The data connection test failed.") from None


@router.get("/data-connections/capabilities")
async def get_data_connection_capabilities(user=Depends(get_admin_user)):
    return {
        provider: {
            "inspect_schema": capabilities.inspect_schema,
            "query": capabilities.query,
            "bounded_operations": list(capabilities.bounded_operations),
            "supports_cancellation": capabilities.supports_cancellation,
            "supports_explain": capabilities.supports_explain,
        }
        for provider, capabilities in adapter_capabilities().items()
    }


@router.get("/tool-profiles", response_model=list[ToolProfileResponse])
async def list_tool_profiles(user=Depends(get_admin_user)):
    return await ControlPlanes.list_profiles()


@router.put("/tool-profiles/{profile_id}", response_model=ToolProfileResponse)
async def upsert_tool_profile(profile_id: str, form: ToolProfileForm, user=Depends(get_admin_user)):
    if profile_id != form.id:
        raise HTTPException(400, "Profile ID does not match the request path.")
    return await ControlPlanes.upsert_profile(form, user.id)


@router.delete("/tool-profiles/{profile_id}")
async def delete_tool_profile(profile_id: str, user=Depends(get_admin_user)):
    if not await ControlPlanes.delete_profile(profile_id):
        raise HTTPException(404, "Tool profile not found.")
    return {"status": "deleted"}


@router.get("/query-audits", response_model=list[QueryAuditResponse])
async def list_query_audits(limit: int = 100, user=Depends(get_admin_user)):
    return await ControlPlanes.list_query_audits(limit)


@router.delete("/query-audits/retention")
async def apply_query_audit_retention(before_epoch: int, user=Depends(get_admin_user)):
    return {"deleted": await ControlPlanes.prune_query_audits(before_epoch)}


@router.get("/tool-profile-audits", response_model=list[ToolProfileAuditResponse])
async def list_tool_profile_audits(limit: int = 100, user=Depends(get_admin_user)):
    return await ControlPlanes.list_profile_audits(limit)
