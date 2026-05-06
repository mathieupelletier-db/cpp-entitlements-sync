"""Tests for domain models."""
from datetime import datetime, timezone

from entitlements_sync.models import (
    AuditRow,
    LFEvent,
    LFEventKind,
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOp,
    SyncOpKind,
)


def test_resource_ref_equality():
    a = ResourceRef(catalog="prod", database="finance", table="trades", column=None)
    b = ResourceRef(catalog="prod", database="finance", table="trades", column=None)
    assert a == b
    assert a.qualified_name == "prod.finance.trades"


def test_resource_ref_with_column():
    r = ResourceRef(catalog="prod", database="finance", table="trades", column="ssn")
    assert r.qualified_name == "prod.finance.trades.ssn"


def test_principal_kinds():
    g = Principal(kind=PrincipalKind.IDP_GROUP, identifier="data-analysts")
    u = Principal(kind=PrincipalKind.IDP_USER, identifier="alice@cpp.example")
    r = Principal(kind=PrincipalKind.IAM_ROLE, identifier="arn:aws:iam::123:role/Analyst")
    assert g.kind == PrincipalKind.IDP_GROUP
    assert u.identifier == "alice@cpp.example"
    assert r.identifier.startswith("arn:")


def test_lf_event_construction():
    ev = LFEvent(
        kind=LFEventKind.GRANT_PERMISSIONS,
        event_id="cloudtrail-event-abc",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef(catalog="prod", database="finance", table="trades", column=None),
        principal=Principal(kind=PrincipalKind.IDP_GROUP, identifier="data-analysts"),
        permissions=("SELECT",),
        lf_tags=None,
    )
    assert ev.kind == LFEventKind.GRANT_PERMISSIONS
    assert ev.permissions == ("SELECT",)


def test_sync_op_kinds():
    op = SyncOp(
        kind=SyncOpKind.GRANT,
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=Principal(PrincipalKind.IDP_GROUP, "data-analysts"),
        permissions=("SELECT",),
        tag_key=None,
        tag_value=None,
        policy_name=None,
    )
    assert op.kind == SyncOpKind.GRANT


def test_audit_row_minimal():
    row = AuditRow(
        ts=datetime.now(timezone.utc),
        source_event_id="cloudtrail-event-abc",
        op_kind=SyncOpKind.GRANT,
        resource_qualified_name="prod.finance.trades",
        principal_identifier="data-analysts",
        status="ok",
        latency_ms=42,
        error=None,
        notes=None,
    )
    assert row.status == "ok"
    assert row.latency_ms == 42
