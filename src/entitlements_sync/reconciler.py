"""Scheduled reconciler — diffs target UC state against current UC state.

In the reconciler-only design this is the sole sync mechanism. It receives a
desired-state snapshot built from Lake Formation (see ``target_builder.py``),
reads the current UC state through the ``UCClient`` interface, emits corrective
``SyncOp``s, and audits each correction with a notes marker
(``reconciler:missing`` or ``reconciler:drift``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .audit import AuditSink
from .models import AuditRow, Principal, PrincipalKind, ResourceRef, SyncOp, SyncOpKind
from .translators.tag import MANAGED_BY_KEY, MANAGED_BY_VALUE
from .uc_client import UCClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetUCState:
    """Desired UC state, post-translation. Tags do NOT include the managed_by marker —
    the reconciler asserts that automatically on every managed resource. Grants principals
    are post-identity-resolution (UC identifiers).

    ``additive_grants`` are grants that must exist for the managed grants to work
    (e.g., USE CATALOG on the catalog for a principal that holds SELECT on a
    table). They are applied if missing but NEVER reverted — the reconciler does
    not claim ownership of the parent resource. Without this distinction, a
    catalog-level reconciler would steamroll every other principal's USE CATALOG
    grant on the same catalog.
    """
    tags: dict[ResourceRef, dict[str, str]]
    grants: dict[ResourceRef, dict[str, set[str]]]
    policies: set[str]
    managed_resources: set[ResourceRef]
    additive_grants: dict[ResourceRef, dict[str, set[str]]] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    missing_ops: int = 0
    drift_ops: int = 0
    audit_rows_written: int = 0


class Reconciler:
    """Diff target UC state vs current UC state and emit corrective ops.

    Uses the ``UCClient`` Protocol — works with ``InMemoryUCClient`` in tests
    and with ``DatabricksUCClient`` in production.
    """

    def __init__(self, *, uc: UCClient, audit: AuditSink) -> None:
        self.uc = uc
        self.audit = audit

    def reconcile(self, target: TargetUCState) -> ReconcileReport:
        report = ReconcileReport()
        self._reconcile_tags(target, report)
        self._reconcile_grants(target, report)
        self._reconcile_additive_grants(target, report)
        self._reconcile_policies(target, report)
        return report

    def _reconcile_additive_grants(
        self, target: TargetUCState, report: ReconcileReport
    ) -> None:
        """Apply ``target.additive_grants`` as missing-only ops.

        These are parent-resource grants (USE CATALOG / USE SCHEMA) synthesized
        to satisfy the UC privilege chain. They are NEVER reverted — the
        reconciler does not claim ownership of the parent resource. Only the
        delta against current state is issued as GRANTs.
        """
        for r, desired_grants in target.additive_grants.items():
            if not desired_grants:
                continue
            actual = self.uc.get_grants(r)
            for principal_id, desired_perms in desired_grants.items():
                actual_perms = actual.get(principal_id, set())
                missing_perms = desired_perms - actual_perms
                if missing_perms:
                    op = SyncOp(
                        kind=SyncOpKind.GRANT, resource=r,
                        principal=Principal(PrincipalKind.IDP_GROUP, principal_id),
                        permissions=tuple(sorted(missing_perms)),
                        tag_key=None, tag_value=None, policy_name=None,
                    )
                    self._apply_correction(op, "reconciler:missing-additive")
                    report.missing_ops += 1
                    report.audit_rows_written += 1

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
        """Apply a corrective op and write an audit row.

        Errors from ``uc.apply`` are caught, audited with ``status=error``, and
        do NOT abort the run. The reconciler is best-effort: one bad op (e.g.,
        a tag value that violates a UC tag policy) must not block all other
        corrections. The error is captured in the audit row for triage.
        """
        resource_name = op.resource.qualified_name if op.resource is not None else "<n/a>"
        principal_name = op.principal.identifier if op.principal is not None else "<n/a>"
        op_summary = f"{op.kind.value:14s} on {resource_name} for {principal_name}"
        try:
            self.uc.apply(op)
            status, error_msg = "ok", None
            log.info("[%s] %s [%s]", status, op_summary, marker)
        except Exception as e:  # noqa: BLE001 — intentional broad catch; full error preserved in audit
            status = "error"
            error_msg = str(e)
            # Errors get a single-line summary at WARNING level; the full SQL/
            # error string lives in the audit row.
            log.warning("[%s] %s [%s] — %s", status, op_summary, marker, _terse_error(error_msg))
        self.audit.write(AuditRow(
            ts=datetime.now(timezone.utc),
            source_event_id="<reconciler>",
            op_kind=op.kind,
            resource_qualified_name=resource_name,
            principal_identifier=principal_name,
            status=status,
            latency_ms=0,
            error=error_msg,
            notes=marker,
        ))


def _terse_error(msg: str, limit: int = 140) -> str:
    """Collapse a multi-line SQL/SDK error to a one-line summary for console output."""
    first_line = msg.splitlines()[0] if msg else ""
    if len(first_line) > limit:
        first_line = first_line[: limit - 1] + "…"
    return first_line
