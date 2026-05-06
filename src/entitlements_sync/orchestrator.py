"""Sync Orchestrator: dispatch one LFEvent through translators -> UC + audit."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .audit import AuditSink
from .identity import IdentityResolver
from .models import AuditRow, LFEvent, LFEventKind, SyncOp, SyncOpKind
from .translators.abac_policy import ABACPolicyTranslator
from .translators.grant import GrantTranslator
from .translators.tag import TagTranslator
from .uc_client import UCClient

GRANT_LIKE = {LFEventKind.GRANT_PERMISSIONS, LFEventKind.REVOKE_PERMISSIONS}
TAG_RESOURCE_LIKE = {LFEventKind.ADD_LFTAGS_TO_RESOURCE, LFEventKind.REMOVE_LFTAGS_FROM_RESOURCE}
TAG_LIFECYCLE_LIKE = {LFEventKind.CREATE_LF_TAG, LFEventKind.UPDATE_LF_TAG, LFEventKind.DELETE_LF_TAG}


class SyncOrchestrator:
    def __init__(
        self,
        *,
        uc: UCClient,
        audit: AuditSink,
        identity: IdentityResolver,
        tag_translator: TagTranslator,
        abac_translator: ABACPolicyTranslator,
        grant_translator: GrantTranslator,
    ) -> None:
        self.uc = uc
        self.audit = audit
        self.identity = identity
        self.tag = tag_translator
        self.abac = abac_translator
        self.grant = grant_translator

    def handle(self, ev: LFEvent) -> None:
        start = time.monotonic()

        if ev.kind in GRANT_LIKE:
            self._handle_grant(ev, start)
        elif ev.kind in TAG_RESOURCE_LIKE:
            self._handle_tag_resource(ev, start)
        elif ev.kind in TAG_LIFECYCLE_LIKE:
            self._handle_tag_lifecycle(ev, start)
        else:
            self._audit_event(
                ev,
                op=None,
                start=start,
                status="unsupported",
                note=f"unknown event kind {ev.kind}",
            )

    def _handle_grant(self, ev: LFEvent, start: float) -> None:
        assert ev.principal is not None
        res = self.identity.resolve(ev.principal)
        if res.status != "ok":
            self._audit_event(ev, op=None, start=start, status="identity_unresolved", note=res.note)
            return

        # rebuild event with resolved principal so translator emits the correct identifier
        resolved_ev = LFEvent(
            kind=ev.kind,
            event_id=ev.event_id,
            event_time=ev.event_time,
            resource=ev.resource,
            principal=res.principal,
            permissions=ev.permissions,
            lf_tags=ev.lf_tags,
        )

        ops, unsupported = self.grant.translate(resolved_ev)
        for op in ops:
            self._apply_and_audit(ev, op, start)
        if unsupported.perms:
            self._audit_event(
                ev,
                op=None,
                start=start,
                status="unsupported",
                note=f"unsupported LF perms: {','.join(unsupported.perms)}",
            )

    def _handle_tag_resource(self, ev: LFEvent, start: float) -> None:
        for op in self.tag.translate(ev):
            self._apply_and_audit(ev, op, start)

    def _handle_tag_lifecycle(self, ev: LFEvent, start: float) -> None:
        for op in self.abac.translate(ev):
            self._apply_and_audit(ev, op, start)

    # --- audit helpers ---

    def _apply_and_audit(self, ev: LFEvent, op: SyncOp, start: float) -> None:
        try:
            self.uc.apply(op)
            self._audit_event(ev, op=op, start=start, status="ok")
        except Exception as exc:  # noqa: BLE001  -- log & continue
            self._audit_event(ev, op=op, start=start, status="error", error=str(exc))

    def _audit_event(
        self,
        ev: LFEvent,
        *,
        op: SyncOp | None,
        start: float,
        status: str,
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        latency_ms = int((time.monotonic() - start) * 1000)
        self.audit.write(
            AuditRow(
                ts=datetime.now(timezone.utc),
                source_event_id=ev.event_id,
                op_kind=op.kind if op is not None else SyncOpKind.NONE,
                resource_qualified_name=(
                    op.resource.qualified_name
                    if op is not None and op.resource is not None
                    else (ev.resource.qualified_name if ev.resource is not None else "<n/a>")
                ),
                principal_identifier=(
                    op.principal.identifier
                    if op is not None and op.principal is not None
                    else (ev.principal.identifier if ev.principal is not None else "<n/a>")
                ),
                status=status,
                latency_ms=latency_ms,
                error=error,
                notes=note,
            )
        )
