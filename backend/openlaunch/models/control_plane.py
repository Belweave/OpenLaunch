"""Persisted generic tool profiles, data connections, and append-only query audits."""

from __future__ import annotations

import time
import uuid
from typing import Any

from openlaunch.internal.db import Base, JSONField, SessionLocal, get_async_db_context
from openlaunch.utils.secret_resolver import inject_connection_secret
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import BigInteger, Boolean, Column, String, Text, delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class DataConnection(Base):
    __tablename__ = "data_connection"
    id = Column(String, primary_key=True)
    scope_type = Column(String, nullable=False, default="instance")
    scope_id = Column(String, nullable=False, default="*")
    provider_type = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    safe_metadata = Column(JSONField, nullable=False, default=dict)
    secret_ref = Column(JSONField, nullable=True)
    policy = Column(JSONField, nullable=False, default=dict)
    access_grants = Column(JSONField, nullable=False, default=list)
    created_by = Column(String, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class ToolProfile(Base):
    __tablename__ = "tool_profile"
    id = Column(String, primary_key=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    assignments = Column(JSONField, nullable=False, default=list)
    bundle = Column(JSONField, nullable=False, default=dict)
    created_by = Column(String, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class QueryAudit(Base):
    __tablename__ = "query_audit"
    id = Column(String, primary_key=True)
    actor_id = Column(String, nullable=False)
    connection_id = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)
    request_id = Column(String, nullable=False, default="")
    tool_call_id = Column(String, nullable=False, default="")
    objects = Column(JSONField, nullable=False, default=list)
    policy_decision = Column(String, nullable=False)
    query_fingerprint = Column(String, nullable=False, default="")
    raw_sql = Column(Text, nullable=True)
    started_at = Column(BigInteger, nullable=False)
    ended_at = Column(BigInteger, nullable=False)
    duration_ms = Column(BigInteger, nullable=False)
    row_count = Column(BigInteger, nullable=False, default=0)
    result_bytes = Column(BigInteger, nullable=False, default=0)
    status = Column(String, nullable=False)


class ToolProfileAudit(Base):
    __tablename__ = "tool_profile_audit"
    id = Column(String, primary_key=True)
    profile_id = Column(String, nullable=False)
    actor_id = Column(String, nullable=False)
    api_credential_id = Column(String, nullable=False, default="")
    request_id = Column(String, nullable=False, default="")
    endpoint = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class DataConnectionForm(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    scope_type: str = "instance"
    scope_id: str = "*"
    provider_type: str
    description: str = ""
    enabled: bool = True
    safe_metadata: dict[str, Any] = Field(default_factory=dict)
    secret_ref: dict[str, Any] | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    access_grants: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_secret_metadata(self):
        forbidden = {
            "url",
            "password",
            "token",
            "private_key",
            "connection_string",
            "redis_url",
            "username",
            "user",
            "config",
            "secret",
            "api_key",
            "key",
        }

        def contains_secret(value):
            if isinstance(value, dict):
                return bool(forbidden.intersection(str(key).lower() for key in value)) or any(
                    contains_secret(item) for item in value.values()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_secret(item) for item in value)
            return False

        if contains_secret(self.safe_metadata):
            raise ValueError("Sensitive connection fields must use a secret reference.")
        if self.scope_type not in {"instance", "organization", "workspace"}:
            raise ValueError("Unsupported connection scope.")
        if self.secret_ref and self.secret_ref.get("type") not in {"env", "file"}:
            raise ValueError("Admin-managed connections support environment or file secret references.")
        if self.secret_ref:
            allowed_reference_keys = (
                {"type", "name", "field"} if self.secret_ref.get("type") == "env" else {"type", "path", "key", "field"}
            )
            if set(self.secret_ref) - allowed_reference_keys:
                raise ValueError("Secret reference contains unsupported fields.")
            field = self.secret_ref.get("field", "url")
            if field not in {
                "url",
                "connection_string",
                "config",
                "password",
                "token",
                "private_key",
            }:
                raise ValueError("Secret reference field is invalid.")
            if self.secret_ref.get("type") == "env" and not self.secret_ref.get("name"):
                raise ValueError("Environment secret references require a name.")
            if self.secret_ref.get("type") == "file" and not self.secret_ref.get("path"):
                raise ValueError("File secret references require a path.")
        return self


class DataConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    scope_type: str
    scope_id: str
    provider_type: str
    description: str
    enabled: bool
    safe_metadata: dict[str, Any]
    has_secret: bool = False
    policy: dict[str, Any]
    access_grants: list[dict[str, Any]]
    created_at: int
    updated_at: int


class ToolProfileForm(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str
    description: str = ""
    enabled: bool = True
    assignments: list[dict[str, str]] = Field(default_factory=list)
    bundle: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_profile(self):
        allowed_scopes = {
            "instance",
            "organization",
            "workspace",
            "user",
            "service_account",
            "api_credential",
            "model",
        }
        if any(item.get("scope_type") not in allowed_scopes or not item.get("scope_id") for item in self.assignments):
            raise ValueError("Tool profile assignment is invalid.")
        allowed_bundle = {"tool_ids", "builtins", "data_source_grants", "empty"}
        if set(self.bundle) - allowed_bundle:
            raise ValueError("Tool profile contains unsupported fields.")
        for key in ("tool_ids", "builtins", "data_source_grants"):
            value = self.bundle.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"Tool profile {key} must be a list of identifiers.")
        if "empty" in self.bundle and not isinstance(self.bundle["empty"], bool):
            raise ValueError("Tool profile empty must be a boolean.")
        return self


class ToolProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    enabled: bool
    assignments: list[dict[str, str]]
    bundle: dict[str, Any]
    created_at: int
    updated_at: int


class QueryAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    actor_id: str
    connection_id: str
    provider_type: str
    request_id: str
    tool_call_id: str
    objects: list[str]
    policy_decision: str
    query_fingerprint: str
    started_at: int
    ended_at: int
    duration_ms: int
    row_count: int
    result_bytes: int
    status: str


class ToolProfileAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    profile_id: str
    actor_id: str
    api_credential_id: str
    request_id: str
    endpoint: str
    outcome: str
    created_at: int


def _connection_response(record: DataConnection) -> DataConnectionResponse:
    return DataConnectionResponse(
        id=record.id,
        scope_type=record.scope_type,
        scope_id=record.scope_id,
        provider_type=record.provider_type,
        description=record.description,
        enabled=record.enabled,
        safe_metadata=record.safe_metadata or {},
        has_secret=bool(record.secret_ref),
        policy=record.policy or {},
        access_grants=record.access_grants or [],
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class ControlPlane:
    @staticmethod
    async def get_api_credential_id(token: str) -> str | None:
        if not token.startswith("sk-"):
            return None
        # Import lazily to keep the control-plane model independent of auth startup.
        from openlaunch.models.users import ApiKey

        async with get_async_db_context() as db:
            return (await db.execute(select(ApiKey.id).where(ApiKey.key == token))).scalar_one_or_none()

    @staticmethod
    async def list_connections(
        db: AsyncSession | None = None,
    ) -> list[DataConnectionResponse]:
        async with get_async_db_context(db) as db:
            records = (await db.execute(select(DataConnection).order_by(DataConnection.id))).scalars().all()
            return [_connection_response(record) for record in records]

    @staticmethod
    async def get_connection(connection_id: str, db: AsyncSession | None = None) -> DataConnection | None:
        async with get_async_db_context(db) as db:
            return await db.get(DataConnection, connection_id)

    @staticmethod
    async def upsert_connection(form: DataConnectionForm, actor_id: str, db: AsyncSession | None = None):
        now = int(time.time())
        async with get_async_db_context(db) as db:
            record = await db.get(DataConnection, form.id)
            if record is None:
                record = DataConnection(
                    id=form.id,
                    created_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(record)
            for key, value in form.model_dump().items():
                if key == "secret_ref" and value is None and record.secret_ref:
                    continue
                setattr(record, key, value)
            record.updated_at = now
            await db.commit()
            await db.refresh(record)
            return _connection_response(record)

    @staticmethod
    async def delete_connection(connection_id: str, db: AsyncSession | None = None) -> bool:
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(DataConnection).where(DataConnection.id == connection_id))
            await db.commit()
            return bool(result.rowcount)

    @staticmethod
    async def list_profiles(
        db: AsyncSession | None = None,
    ) -> list[ToolProfileResponse]:
        async with get_async_db_context(db) as db:
            records = (await db.execute(select(ToolProfile).order_by(ToolProfile.id))).scalars().all()
            return [ToolProfileResponse.model_validate(record) for record in records]

    @staticmethod
    async def upsert_profile(form: ToolProfileForm, actor_id: str, db: AsyncSession | None = None):
        now = int(time.time())
        async with get_async_db_context(db) as db:
            record = await db.get(ToolProfile, form.id)
            if record is None:
                record = ToolProfile(id=form.id, created_by=actor_id, created_at=now, updated_at=now)
                db.add(record)
            for key, value in form.model_dump().items():
                setattr(record, key, value)
            record.updated_at = now
            await db.commit()
            await db.refresh(record)
            return ToolProfileResponse.model_validate(record)

    @staticmethod
    async def delete_profile(profile_id: str, db: AsyncSession | None = None) -> bool:
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(ToolProfile).where(ToolProfile.id == profile_id))
            await db.commit()
            return bool(result.rowcount)

    @staticmethod
    async def resolve_profile(
        profile_id: str,
        *,
        user_id: str,
        model_id: str,
        scopes: dict[str, str] | None = None,
    ):
        async with get_async_db_context() as db:
            profile = await db.get(ToolProfile, profile_id)
            if profile is None or not profile.enabled:
                return None
            principals = {
                "instance": "*",
                "user": user_id,
                "model": model_id,
                **(scopes or {}),
            }
            allowed = any(
                assignment.get("scope_type") in principals
                and assignment.get("scope_id") == principals[assignment["scope_type"]]
                for assignment in (profile.assignments or [])
            )
            return dict(profile.bundle or {}) if allowed else None

    @staticmethod
    async def append_query_audit(event: dict[str, Any], db: AsyncSession | None = None) -> None:
        async with get_async_db_context(db) as db:
            db.add(QueryAudit(id=str(uuid.uuid4()), **event))
            await db.commit()

    @staticmethod
    async def append_profile_audit(event: dict[str, Any], db: AsyncSession | None = None) -> None:
        async with get_async_db_context(db) as db:
            db.add(ToolProfileAudit(id=str(uuid.uuid4()), created_at=int(time.time()), **event))
            await db.commit()

    @staticmethod
    async def list_profile_audits(limit: int = 100, db: AsyncSession | None = None) -> list[ToolProfileAuditResponse]:
        async with get_async_db_context(db) as db:
            records = (
                (
                    await db.execute(
                        select(ToolProfileAudit)
                        .order_by(ToolProfileAudit.created_at.desc())
                        .limit(min(1000, max(1, limit)))
                    )
                )
                .scalars()
                .all()
            )
            return [ToolProfileAuditResponse.model_validate(record) for record in records]

    @staticmethod
    async def list_query_audits(limit: int = 100, db: AsyncSession | None = None) -> list[QueryAuditResponse]:
        async with get_async_db_context(db) as db:
            records = (
                (
                    await db.execute(
                        select(QueryAudit).order_by(QueryAudit.started_at.desc()).limit(min(1000, max(1, limit)))
                    )
                )
                .scalars()
                .all()
            )
            return [QueryAuditResponse.model_validate(record) for record in records]

    @staticmethod
    async def prune_query_audits(before_epoch: int, db: AsyncSession | None = None) -> int:
        """Retention-only deletion; ordinary audit records have no mutation API."""
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(QueryAudit).where(QueryAudit.ended_at < before_epoch))
            await db.commit()
            return int(result.rowcount or 0)


def get_persisted_runtime_connections() -> list[dict[str, Any]]:
    """Read enabled records for runtime tools; secrets exist only in the returned copies."""
    with SessionLocal() as db:
        records = db.execute(select(DataConnection).where(DataConnection.enabled.is_(True))).scalars().all()
        result = []
        for record in records:
            connection = {
                **inject_connection_secret(record.safe_metadata or {}, record.secret_ref),
                "id": record.id,
                "type": record.provider_type,
                "description": record.description,
                "scope_type": record.scope_type,
                "scope_id": record.scope_id,
                "access_grants": record.access_grants or [],
                "policy": record.policy or {},
            }
            result.append(connection)
        return result


ControlPlanes = ControlPlane()
