"""SQL-INSERT-backed audit sink.

Writes audit rows through a ``SQLRunner`` (e.g., the Statement Execution API),
so the reconciler can run from anywhere the Databricks SDK works — including a
laptop — with no pyspark dependency.

The Delta-table schema is kept literal here (not factored into a separate
module) so it's grep-able from the AI/BI dashboard query.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .audit import AuditSink
from .databricks_uc import SQLRunner
from .models import AuditRow

log = logging.getLogger(__name__)


# Mirrors AuditRow. The AI/BI Drift Dashboard binds against this schema.
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


# Order MUST match the column order in AUDIT_SCHEMA_DDL.
_COLUMN_ORDER: tuple[str, ...] = (
    "ts",
    "source_event_id",
    "op_kind",
    "resource_qualified_name",
    "principal_identifier",
    "status",
    "latency_ms",
    "error",
    "notes",
)


@dataclass
class SQLAuditSink(AuditSink):
    sql: SQLRunner
    table_name: str
    _table_ensured: bool = False

    def write(self, row: AuditRow) -> None:
        self._ensure_table()
        self.sql(_render_insert(self.table_name, row))

    def _ensure_table(self) -> None:
        if self._table_ensured:
            return
        # Bootstrap parent schema if needed. The catalog is assumed to already
        # exist with proper ACLs — schemas are cheap and reconciler-friendly to
        # auto-create on first write.
        catalog, schema, _table = self.table_name.split(".", 2)
        log.info("bootstrapping audit table %s", self.table_name)
        self.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
        self.sql(
            f"CREATE TABLE IF NOT EXISTS {self.table_name} "
            f"({AUDIT_SCHEMA_DDL}) USING DELTA"
        )
        self._table_ensured = True


def _render_insert(table_name: str, row: AuditRow) -> str:
    values = ", ".join(_render_value(_field_value(row, col)) for col in _COLUMN_ORDER)
    return f"INSERT INTO {table_name} VALUES ({values})"


def _field_value(row: AuditRow, name: str) -> Any:
    val = getattr(row, name)
    if name == "op_kind":
        return val.value  # enum -> string
    return val


def _render_value(val: Any) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, datetime):
        return f"TIMESTAMP '{val.isoformat(sep=' ')}'"
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    raise TypeError(f"Unsupported audit value type: {type(val).__name__}")
