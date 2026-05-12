"""Translate LF GrantPermissions/RevokePermissions events into UC GRANT/REVOKE ops.

Uses ``privilege_mapping.map_lf_to_uc_privileges`` for the level-aware LF -> UC
translation so the event path and the reconciler target builder stay aligned.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import LFEvent, LFEventKind, SyncOp, SyncOpKind
from ..privilege_mapping import map_lf_to_uc_privileges


@dataclass(frozen=True)
class UnsupportedPermissions:
    perms: tuple[str, ...]


class GrantTranslator:
    """LF resource grant/revoke -> UC GRANT/REVOKE op (one op per event, perms tupled)."""

    def translate(self, ev: LFEvent) -> tuple[list[SyncOp], UnsupportedPermissions]:
        if ev.kind not in (LFEventKind.GRANT_PERMISSIONS, LFEventKind.REVOKE_PERMISSIONS):
            return [], UnsupportedPermissions(perms=())

        assert ev.resource is not None and ev.principal is not None

        uc_perms, unsupported = map_lf_to_uc_privileges(ev.permissions, ev.resource)

        if not uc_perms:
            return [], UnsupportedPermissions(perms=tuple(unsupported))

        op_kind = SyncOpKind.GRANT if ev.kind is LFEventKind.GRANT_PERMISSIONS else SyncOpKind.REVOKE
        op = SyncOp(
            kind=op_kind,
            resource=ev.resource,
            principal=ev.principal,
            permissions=tuple(sorted(uc_perms)),
            tag_key=None,
            tag_value=None,
            policy_name=None,
        )
        return [op], UnsupportedPermissions(perms=tuple(unsupported))
