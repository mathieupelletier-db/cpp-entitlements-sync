"""Tests for SQLAuditSink — no pyspark involved."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from entitlements_sync.delta_audit import AUDIT_SCHEMA_DDL
from entitlements_sync.models import AuditRow, SyncOpKind
from entitlements_sync.sql_audit import SQLAuditSink


@dataclass
class RecordingSQLRunner:
    statements: list[str] = field(default_factory=list)

    def __call__(self, statement: str) -> list[list[str]]:
        self.statements.append(statement)
        return []


def _row(**overrides) -> AuditRow:
    base = {
        "ts": datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc),
        "source_event_id": "<reconciler>",
        "op_kind": SyncOpKind.SET_TAG,
        "resource_qualified_name": "main.finance.trades",
        "principal_identifier": "<n/a>",
        "status": "ok",
        "latency_ms": 0,
        "error": None,
        "notes": "reconciler:missing",
    }
    base.update(overrides)
    return AuditRow(**base)


def test_first_write_creates_schema_table_then_inserts():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row())

    assert len(runner.statements) == 3
    schema_stmt = runner.statements[0]
    assert "CREATE SCHEMA IF NOT EXISTS main.sync_audit" in schema_stmt

    create = runner.statements[1]
    assert "CREATE TABLE IF NOT EXISTS main.sync_audit.events" in create
    assert AUDIT_SCHEMA_DDL in create
    assert "USING DELTA" in create

    insert = runner.statements[2]
    assert insert.startswith("INSERT INTO main.sync_audit.events VALUES (")


def test_subsequent_writes_skip_bootstrap():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row())
    sink.write(_row())
    sink.write(_row())
    # 1 CREATE SCHEMA + 1 CREATE TABLE + 3 INSERTs
    assert sum(1 for s in runner.statements if "CREATE SCHEMA" in s) == 1
    assert sum(1 for s in runner.statements if "CREATE TABLE" in s) == 1
    assert sum(1 for s in runner.statements if s.startswith("INSERT")) == 3


def test_insert_includes_all_nine_columns_in_order():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row())
    insert = runner.statements[2]

    # values clause should mention each value in the same order as AUDIT_SCHEMA_DDL
    assert "TIMESTAMP '2026-05-11 10:00:00+00:00'" in insert
    assert "'<reconciler>'" in insert
    assert "'set_tag'" in insert
    assert "'main.finance.trades'" in insert
    assert "'<n/a>'" in insert
    assert "'ok'" in insert
    assert ", 0," in insert  # latency_ms
    assert ", NULL," in insert  # error is None
    assert "'reconciler:missing'" in insert


def test_null_for_optional_columns():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row(error=None, notes=None))
    insert = runner.statements[2]
    # Both error AND notes should appear as NULL
    assert insert.count("NULL") >= 2


def test_string_with_apostrophe_is_sql_escaped():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row(notes="reconciler's note"))
    insert = runner.statements[2]
    assert "'reconciler''s note'" in insert


def test_op_kind_is_serialized_as_lowercase_string():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row(op_kind=SyncOpKind.UPSERT_POLICY))
    insert = runner.statements[2]
    assert "'upsert_policy'" in insert


def test_timestamp_without_tz_still_serializes():
    runner = RecordingSQLRunner()
    sink = SQLAuditSink(sql=runner, table_name="main.sync_audit.events")
    sink.write(_row(ts=datetime(2026, 5, 11, 10, 0, 0)))
    insert = runner.statements[2]
    assert "TIMESTAMP '2026-05-11 10:00:00'" in insert
