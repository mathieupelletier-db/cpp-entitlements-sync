"""Domain models for the LF -> UC entitlements sync engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PrincipalKind(str, Enum):
    IDP_GROUP = "idp_group"
    IDP_USER = "idp_user"
    IAM_ROLE = "iam_role"


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    identifier: str  # group display name, email/UPN, or IAM ARN


@dataclass(frozen=True)
class ResourceRef:
    catalog: str
    database: str
    table: Optional[str]
    column: Optional[str]

    @property
    def qualified_name(self) -> str:
        parts = [self.catalog, self.database]
        if self.table is not None:
            parts.append(self.table)
        if self.column is not None:
            parts.append(self.column)
        return ".".join(parts)


class LFEventKind(str, Enum):
    GRANT_PERMISSIONS = "GrantPermissions"
    REVOKE_PERMISSIONS = "RevokePermissions"
    ADD_LFTAGS_TO_RESOURCE = "AddLFTagsToResource"
    REMOVE_LFTAGS_FROM_RESOURCE = "RemoveLFTagsFromResource"
    CREATE_LF_TAG = "CreateLFTag"
    UPDATE_LF_TAG = "UpdateLFTag"
    DELETE_LF_TAG = "DeleteLFTag"


@dataclass(frozen=True)
class LFTagAssignment:
    key: str
    value: str


@dataclass(frozen=True)
class LFEvent:
    kind: LFEventKind
    event_id: str
    event_time: datetime
    resource: Optional[ResourceRef]
    principal: Optional[Principal]
    permissions: tuple[str, ...]  # e.g., ("SELECT",) ("SELECT", "DESCRIBE")
    lf_tags: Optional[tuple[LFTagAssignment, ...]]


class SyncOpKind(str, Enum):
    SET_TAG = "set_tag"
    UNSET_TAG = "unset_tag"
    UPSERT_POLICY = "upsert_policy"
    DELETE_POLICY = "delete_policy"
    GRANT = "grant"
    REVOKE = "revoke"


@dataclass(frozen=True)
class SyncOp:
    kind: SyncOpKind
    resource: Optional[ResourceRef]
    principal: Optional[Principal]
    permissions: tuple[str, ...]
    tag_key: Optional[str]
    tag_value: Optional[str]
    policy_name: Optional[str]


@dataclass(frozen=True)
class AuditRow:
    ts: datetime
    source_event_id: str
    op_kind: SyncOpKind
    resource_qualified_name: str
    principal_identifier: str
    status: str  # "ok" | "identity_unresolved" | "unsupported" | "error"
    latency_ms: int
    error: Optional[str]
    notes: Optional[str]
