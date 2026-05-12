"""Tests for DeltaAuditSink. Uses a recording stub SparkSession; never imports pyspark."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from entitlements_sync.delta_audit import AUDIT_SCHEMA_DDL, DeltaAuditSink
from entitlements_sync.models import AuditRow, SyncOpKind


# --- Spark stubs ------------------------------------------------------------


@dataclass
class _StubWriter:
    parent: "_StubDataFrame"
    format_: str = ""
    mode_: str = ""

    def format(self, fmt: str) -> "_StubWriter":
        self.format_ = fmt
        return self

    def mode(self, mode: str) -> "_StubWriter":
        self.mode_ = mode
        return self

    def saveAsTable(self, name: str) -> None:
        self.parent.saved_calls.append((self.format_, self.mode_, name))


@dataclass
class _StubDataFrame:
    payloads: list[dict[str, Any]]
    saved_calls: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def write(self) -> _StubWriter:
        return _StubWriter(parent=self)


@dataclass
class _StubSpark:
    sql_calls: list[str] = field(default_factory=list)
    created_frames: list[_StubDataFrame] = field(default_factory=list)

    def sql(self, statement: str) -> None:
        self.sql_calls.append(statement)

    def createDataFrame(self, payloads: list[dict[str, Any]]) -> _StubDataFrame:
        df = _StubDataFrame(payloads=payloads)
        self.created_frames.append(df)
        return df


# --- helpers ---------------------------------------------------------------


def _row() -> AuditRow:
    return AuditRow(
        ts=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        source_event_id="<reconciler>",
        op_kind=SyncOpKind.SET_TAG,
        resource_qualified_name="main.finance.trades",
        principal_identifier="<n/a>",
        status="ok",
        latency_ms=0,
        error=None,
        notes="reconciler:missing",
    )


# --- tests -----------------------------------------------------------------


def test_first_write_creates_table_then_appends():
    spark = _StubSpark()
    sink = DeltaAuditSink(spark=spark, table_name="main.sync_audit.events")
    sink.write(_row())

    # CREATE TABLE was issued once on first write
    assert len(spark.sql_calls) == 1
    create = spark.sql_calls[0]
    assert "CREATE TABLE IF NOT EXISTS main.sync_audit.events" in create
    assert AUDIT_SCHEMA_DDL in create
    assert "USING DELTA" in create

    # Then a Delta append was performed against the same table
    assert len(spark.created_frames) == 1
    saved = spark.created_frames[0].saved_calls
    assert saved == [("delta", "append", "main.sync_audit.events")]


def test_subsequent_writes_skip_create_table():
    spark = _StubSpark()
    sink = DeltaAuditSink(spark=spark, table_name="main.sync_audit.events")
    sink.write(_row())
    sink.write(_row())
    sink.write(_row())
    # CREATE TABLE only on first call
    assert len(spark.sql_calls) == 1
    # Three appends
    assert sum(len(df.saved_calls) for df in spark.created_frames) == 3


def test_payload_columns_match_schema():
    spark = _StubSpark()
    sink = DeltaAuditSink(spark=spark, table_name="main.sync_audit.events")
    sink.write(_row())

    payload = spark.created_frames[0].payloads[0]
    expected_keys = {
        "ts",
        "source_event_id",
        "op_kind",
        "resource_qualified_name",
        "principal_identifier",
        "status",
        "latency_ms",
        "error",
        "notes",
    }
    assert set(payload.keys()) == expected_keys


def test_op_kind_serialized_as_string():
    spark = _StubSpark()
    sink = DeltaAuditSink(spark=spark, table_name="main.sync_audit.events")
    sink.write(_row())
    payload = spark.created_frames[0].payloads[0]
    assert payload["op_kind"] == "set_tag"
    assert isinstance(payload["op_kind"], str)
