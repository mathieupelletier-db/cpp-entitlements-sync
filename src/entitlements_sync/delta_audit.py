"""Delta-backed AuditSink.

Writes one Delta row per ``AuditRow``. The target UC table is created (with the
expected schema) on first write if it does not already exist; subsequent writes
append. The SparkSession is injected so the module is testable without pyspark
installed: tests pass a recording stub; production passes the real session.

Schema:

    ts                      TIMESTAMP
    source_event_id         STRING
    op_kind                 STRING
    resource_qualified_name STRING
    principal_identifier    STRING
    status                  STRING
    latency_ms              BIGINT
    error                   STRING
    notes                   STRING

This is the shape the AI/BI Drift Dashboard reads.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .audit import AuditSink
from .models import AuditRow


# Mirrors AuditRow. Kept literal so it is grep-able from the dashboard query.
AUDIT_SCHEMA_DDL = (
    "ts TIMESTAMP, "
    "source_event_id STRING, "
    "op_kind STRING, "
    "resource_qualified_name STRING, "
    "principal_identifier STRING, "
    "status STRING, "
    "latency_ms BIGINT, "
    "error STRING, "
    "notes STRING"
)


@dataclass
class DeltaAuditSink(AuditSink):
    """Append-only audit sink backed by a Delta table in Unity Catalog."""

    spark: Any  # pyspark.sql.SparkSession
    table_name: str  # e.g., "main.sync_audit.events" — fully qualified
    _table_ensured: bool = False

    def write(self, row: AuditRow) -> None:
        self._ensure_table()
        payload = _row_to_dict(row)
        df = self.spark.createDataFrame([payload])
        df.write.format("delta").mode("append").saveAsTable(self.table_name)

    def _ensure_table(self) -> None:
        if self._table_ensured:
            return
        self.spark.sql(
            f"CREATE TABLE IF NOT EXISTS {self.table_name} "
            f"({AUDIT_SCHEMA_DDL}) USING DELTA"
        )
        self._table_ensured = True


def _row_to_dict(row: AuditRow) -> dict[str, Any]:
    """Serialize an AuditRow into a dict whose keys and types match the Delta schema."""
    d = asdict(row)
    # asdict turns the enum into the enum value automatically because SyncOpKind
    # is a str subclass, but be explicit so the contract is obvious.
    d["op_kind"] = row.op_kind.value
    return d
