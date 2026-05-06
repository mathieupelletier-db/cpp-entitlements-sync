"""Audit sink. The Delta-backed implementation is in Plan 3."""
from __future__ import annotations

from typing import Protocol

from .models import AuditRow


class AuditSink(Protocol):
    def write(self, row: AuditRow) -> None:
        ...


class InMemoryAuditSink:
    """Append-only in-memory audit sink. Test/POC only — Plan 3 swaps in a Delta-backed sink."""

    def __init__(self) -> None:
        self.rows: list[AuditRow] = []

    def write(self, row: AuditRow) -> None:
        self.rows.append(row)

    def filter(self, *, status: str | None = None) -> list[AuditRow]:
        return [r for r in self.rows if status is None or r.status == status]
