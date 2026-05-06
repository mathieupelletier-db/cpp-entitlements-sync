"""Tests for the audit sink."""
from datetime import datetime, timezone

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.models import AuditRow, SyncOpKind


def _row(**overrides):
    base = dict(
        ts=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        source_event_id="evt",
        op_kind=SyncOpKind.GRANT,
        resource_qualified_name="prod.finance.trades",
        principal_identifier="data-analysts",
        status="ok",
        latency_ms=0,
        error=None,
        notes=None,
    )
    base.update(overrides)
    return AuditRow(**base)


def test_audit_records_and_lists():
    sink = InMemoryAuditSink()
    row = _row(latency_ms=10)
    sink.write(row)
    assert sink.rows == [row]


def test_audit_filters_by_status():
    sink = InMemoryAuditSink()
    sink.write(_row(status="ok"))
    sink.write(_row(status="identity_unresolved"))
    sink.write(_row(status="ok"))
    assert len(sink.filter(status="ok")) == 2
    assert len(sink.filter(status="identity_unresolved")) == 1


def test_filter_with_no_status_returns_all():
    sink = InMemoryAuditSink()
    sink.write(_row(status="ok"))
    sink.write(_row(status="error"))
    assert len(sink.filter()) == 2
