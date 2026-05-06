"""Scheduled reconciler — diffs target UC state against current UC state.

The reconciler is the safety net for the event-driven sync path. It pulls a desired-state
snapshot (computed externally from Lake Formation), reads the current UC state via the
UCClient inspection helpers, emits corrective SyncOps, and audits each correction with
a notes marker (`reconciler:missing` or `reconciler:drift`).

For Plan 1 scope, the target snapshot is a dataclass passed in by the caller. Plan 3
will add the LF-side puller that constructs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import AuditSink
from .models import AuditRow, Principal, PrincipalKind, ResourceRef, SyncOp, SyncOpKind
from .translators.tag import MANAGED_BY_KEY, MANAGED_BY_VALUE
from .uc_client import InMemoryUCClient


@dataclass(frozen=True)
class TargetUCState:
    """Desired UC state, post-translation. Tags do NOT include the managed_by marker —
    the reconciler asserts that automatically on every managed resource. Grants principals
    are post-identity-resolution (UC identifiers)."""
    tags: dict[ResourceRef, dict[str, str]]
    grants: dict[ResourceRef, dict[str, set[str]]]
    policies: set[str]
    managed_resources: set[ResourceRef]


@dataclass
class ReconcileReport:
    missing_ops: int = 0
    drift_ops: int = 0
    audit_rows_written: int = 0


class Reconciler:
    """Diff target UC state vs current UC state and emit corrective ops.

    Currently relies on InMemoryUCClient's inspection helpers. The Plan 2 SDK adapter
    will need an equivalent read interface (e.g., list_tags_for(resource), list_grants_for, list_policies).
    """

    def __init__(self, *, uc: InMemoryUCClient, audit: AuditSink) -> None:
        self.uc = uc
        self.audit = audit

    def reconcile(self, target: TargetUCState) -> ReconcileReport:
        report = ReconcileReport()
        self._reconcile_tags(target, report)
        self._reconcile_grants(target, report)
        self._reconcile_policies(target, report)
        return report

    def _reconcile_tags(self, target: TargetUCState, report: ReconcileReport) -> None:
        # For each managed resource, compare desired tags + managed_by marker against actual
        for r in target.managed_resources:
            desired = dict(target.tags.get(r, {}))
            desired[MANAGED_BY_KEY] = MANAGED_BY_VALUE  # marker is always implicit
            actual = self.uc.get_tags(r)

            for k, v in desired.items():
                if actual.get(k) != v:
                    op = SyncOp(
                        kind=SyncOpKind.SET_TAG, resource=r, principal=None,
                        permissions=(), tag_key=k, tag_value=v, policy_name=None,
                    )
                    self._apply_correction(op, "reconciler:missing")
                    report.missing_ops += 1
                    report.audit_rows_written += 1

            # Drift: tags in actual that are not in desired
            for k in actual:
                if k not in desired:
                    op = SyncOp(
                        kind=SyncOpKind.UNSET_TAG, resource=r, principal=None,
                        permissions=(), tag_key=k, tag_value=None, policy_name=None,
                    )
                    self._apply_correction(op, "reconciler:drift")
                    report.drift_ops += 1
                    report.audit_rows_written += 1

    def _reconcile_grants(self, target: TargetUCState, report: ReconcileReport) -> None:
        # Walk every managed resource. Compare desired grants vs actual.
        for r in target.managed_resources:
            desired = target.grants.get(r, {})
            actual = self.uc.get_grants(r)

            # Missing: principals/perms in desired but not actual
            for principal_id, perms in desired.items():
                actual_perms = actual.get(principal_id, set())
                missing_perms = perms - actual_perms
                if missing_perms:
                    op = SyncOp(
                        kind=SyncOpKind.GRANT, resource=r,
                        principal=Principal(PrincipalKind.IDP_GROUP, principal_id),
                        permissions=tuple(sorted(missing_perms)),
                        tag_key=None, tag_value=None, policy_name=None,
                    )
                    self._apply_correction(op, "reconciler:missing")
                    report.missing_ops += 1
                    report.audit_rows_written += 1

            # Drift: principals/perms in actual but not in desired
            for principal_id, perms in actual.items():
                desired_perms = desired.get(principal_id, set())
                drift_perms = perms - desired_perms
                if drift_perms:
                    op = SyncOp(
                        kind=SyncOpKind.REVOKE, resource=r,
                        principal=Principal(PrincipalKind.IDP_GROUP, principal_id),
                        permissions=tuple(sorted(drift_perms)),
                        tag_key=None, tag_value=None, policy_name=None,
                    )
                    self._apply_correction(op, "reconciler:drift")
                    report.drift_ops += 1
                    report.audit_rows_written += 1

    def _reconcile_policies(self, target: TargetUCState, report: ReconcileReport) -> None:
        actual = self.uc.get_policies()
        for name in target.policies - actual:
            op = SyncOp(
                kind=SyncOpKind.UPSERT_POLICY, resource=None, principal=None,
                permissions=(), tag_key=None, tag_value=None, policy_name=name,
            )
            self._apply_correction(op, "reconciler:missing")
            report.missing_ops += 1
            report.audit_rows_written += 1
        for name in actual - target.policies:
            op = SyncOp(
                kind=SyncOpKind.DELETE_POLICY, resource=None, principal=None,
                permissions=(), tag_key=None, tag_value=None, policy_name=name,
            )
            self._apply_correction(op, "reconciler:drift")
            report.drift_ops += 1
            report.audit_rows_written += 1

    def _apply_correction(self, op: SyncOp, marker: str) -> None:
        self.uc.apply(op)
        self.audit.write(AuditRow(
            ts=datetime.now(timezone.utc),
            source_event_id="<reconciler>",
            op_kind=op.kind,
            resource_qualified_name=op.resource.qualified_name if op.resource is not None else "<n/a>",
            principal_identifier=op.principal.identifier if op.principal is not None else "<n/a>",
            status="ok",
            latency_ms=0,
            error=None,
            notes=marker,
        ))
