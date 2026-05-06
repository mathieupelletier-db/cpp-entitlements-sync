"""Translate LF GrantPermissions/RevokePermissions events into UC GRANT/REVOKE ops."""
from __future__ import annotations

from dataclasses import dataclass

from ..models import LFEvent, LFEventKind, SyncOp, SyncOpKind

UC_SUPPORTED_PRIVILEGES: frozenset[str] = frozenset(
    {"SELECT", "DESCRIBE", "ALTER", "INSERT", "DELETE", "DROP"}
)


@dataclass(frozen=True)
class UnsupportedPermissions:
    perms: tuple[str, ...]


class GrantTranslator:
    """LF resource grant/revoke -> UC GRANT/REVOKE op (one op per event, perms tupled)."""

    def translate(self, ev: LFEvent) -> tuple[list[SyncOp], UnsupportedPermissions]:
        if ev.kind not in (LFEventKind.GRANT_PERMISSIONS, LFEventKind.REVOKE_PERMISSIONS):
            return [], UnsupportedPermissions(perms=())

        assert ev.resource is not None and ev.principal is not None

        supported = tuple(p for p in ev.permissions if p in UC_SUPPORTED_PRIVILEGES)
        unsupported = tuple(p for p in ev.permissions if p not in UC_SUPPORTED_PRIVILEGES)

        if not supported:
            return [], UnsupportedPermissions(perms=unsupported)

        op_kind = SyncOpKind.GRANT if ev.kind is LFEventKind.GRANT_PERMISSIONS else SyncOpKind.REVOKE
        op = SyncOp(
            kind=op_kind,
            resource=ev.resource,
            principal=ev.principal,
            permissions=supported,
            tag_key=None,
            tag_value=None,
            policy_name=None,
        )
        return [op], UnsupportedPermissions(perms=unsupported)
