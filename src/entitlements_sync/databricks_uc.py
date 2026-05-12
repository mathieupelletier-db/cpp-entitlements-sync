"""Databricks-backed UCClient.

Writes are issued as SQL via an injected ``SQLRunner`` callable; reads come
through the same runner. Keeping the runner as a Callable decouples this
module from the Databricks SDK surface (which is broad and evolves quickly)
and makes unit tests trivial: the test passes a stub runner that records the
SQL it was asked to run.

ABAC policies sit behind a separate ``ABACPolicyAPI`` interface. UC ABAC is a
moving preview surface; the default ``LoggingABACPolicyAPI`` simply logs and
tracks names in memory, which is enough for the POC reconciler to exercise
the orchestration. When the workspace REST API is wired up, drop in a real
implementation without changing the ``DatabricksUCClient``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .models import ResourceRef, SyncOp, SyncOpKind
from .uc_client import UCClient

log = logging.getLogger(__name__)

# A SQLRunner takes a SQL statement and returns the rows (list of cells). Empty
# list for statements that produce no rows (DDL, GRANT, REVOKE).
SQLRunner = Callable[[str], list[list[str]]]


# --------------------------- ABAC policy surface ---------------------------


class ABACPolicyAPI(Protocol):
    def upsert(self, policy_name: str) -> None: ...
    def delete(self, policy_name: str) -> None: ...
    def list(self) -> set[str]: ...


@dataclass
class LoggingABACPolicyAPI:
    """Default: track names in memory + log. Swap in the real REST adapter when
    UC ABAC stabilizes (deferred — see project memory)."""
    _policies: set[str] = field(default_factory=set)

    def upsert(self, policy_name: str) -> None:
        log.info("ABAC upsert: %s", policy_name)
        self._policies.add(policy_name)

    def delete(self, policy_name: str) -> None:
        log.info("ABAC delete: %s", policy_name)
        self._policies.discard(policy_name)

    def list(self) -> set[str]:
        return set(self._policies)


# --------------------------- helpers ---------------------------------------


def _quote_sql_literal(value: str) -> str:
    """Single-quote a value for inclusion in a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    """Backtick-quote an identifier."""
    return "`" + name.replace("`", "``") + "`"


def _object_type_and_qualified(r: ResourceRef) -> tuple[str, str]:
    """Return (UC object type keyword, fully-qualified back-ticked name)."""
    if r.column is not None:
        # Column-level objects are addressed via the table; column tags use
        # ALTER TABLE ... ALTER COLUMN ... SET TAGS. Column-level grants in
        # UC are coarse and rarely used; out of POC.
        raise NotImplementedError("Column-level objects not supported in POC")
    if r.table is not None:
        return "TABLE", ".".join(_quote_ident(p) for p in (r.catalog, r.database, r.table))
    if r.database:
        return "SCHEMA", ".".join(_quote_ident(p) for p in (r.catalog, r.database))
    return "CATALOG", _quote_ident(r.catalog)


# --------------------------- the UC client ---------------------------------


@dataclass
class DatabricksUCClient(UCClient):
    sql: SQLRunner
    abac: ABACPolicyAPI = field(default_factory=LoggingABACPolicyAPI)

    # ---- write path ----

    def apply(self, op: SyncOp) -> None:
        if op.kind is SyncOpKind.SET_TAG:
            self._exec_set_tag(op)
        elif op.kind is SyncOpKind.UNSET_TAG:
            self._exec_unset_tag(op)
        elif op.kind is SyncOpKind.GRANT:
            self._exec_grant(op)
        elif op.kind is SyncOpKind.REVOKE:
            self._exec_revoke(op)
        elif op.kind is SyncOpKind.UPSERT_POLICY:
            assert op.policy_name is not None
            self.abac.upsert(op.policy_name)
        elif op.kind is SyncOpKind.DELETE_POLICY:
            assert op.policy_name is not None
            self.abac.delete(op.policy_name)
        elif op.kind is SyncOpKind.NONE:
            return
        else:
            raise ValueError(f"Unknown SyncOpKind: {op.kind}")

    def _run(self, statement: str) -> list[list[str]]:
        """Single point that issues SQL — logs every statement at DEBUG."""
        log.debug("SQL: %s", statement)
        return self.sql(statement)

    def _exec_set_tag(self, op: SyncOp) -> None:
        assert op.resource is not None and op.tag_key is not None and op.tag_value is not None
        obj_type, qualified = _object_type_and_qualified(op.resource)
        self._run(
            f"ALTER {obj_type} {qualified} "
            f"SET TAGS ({_quote_sql_literal(op.tag_key)} = {_quote_sql_literal(op.tag_value)})"
        )

    def _exec_unset_tag(self, op: SyncOp) -> None:
        assert op.resource is not None and op.tag_key is not None
        obj_type, qualified = _object_type_and_qualified(op.resource)
        self._run(
            f"ALTER {obj_type} {qualified} "
            f"UNSET TAGS ({_quote_sql_literal(op.tag_key)})"
        )

    def _exec_grant(self, op: SyncOp) -> None:
        assert op.resource is not None and op.principal is not None and op.permissions
        obj_type, qualified = _object_type_and_qualified(op.resource)
        perms = ", ".join(op.permissions)
        self._run(
            f"GRANT {perms} ON {obj_type} {qualified} "
            f"TO {_quote_ident(op.principal.identifier)}"
        )

    def _exec_revoke(self, op: SyncOp) -> None:
        assert op.resource is not None and op.principal is not None and op.permissions
        obj_type, qualified = _object_type_and_qualified(op.resource)
        perms = ", ".join(op.permissions)
        self._run(
            f"REVOKE {perms} ON {obj_type} {qualified} "
            f"FROM {_quote_ident(op.principal.identifier)}"
        )

    # ---- read path ----

    def get_tags(self, r: ResourceRef) -> dict[str, str]:
        """Read effective UC tags on ``r``.

        Uses ``information_schema`` views so we always get a flat (key, value)
        result regardless of resource depth. The reader filters down to the
        single resource by name.
        """
        if r.table is not None:
            sql = (
                "SELECT tag_name, tag_value FROM system.information_schema.table_tags "
                f"WHERE catalog_name = {_quote_sql_literal(r.catalog)} "
                f"AND schema_name = {_quote_sql_literal(r.database)} "
                f"AND table_name = {_quote_sql_literal(r.table)}"
            )
        elif r.database:
            sql = (
                "SELECT tag_name, tag_value FROM system.information_schema.schema_tags "
                f"WHERE catalog_name = {_quote_sql_literal(r.catalog)} "
                f"AND schema_name = {_quote_sql_literal(r.database)}"
            )
        else:
            sql = (
                "SELECT tag_name, tag_value FROM system.information_schema.catalog_tags "
                f"WHERE catalog_name = {_quote_sql_literal(r.catalog)}"
            )
        rows = self._run(sql)
        return {row[0]: row[1] for row in rows if row and len(row) >= 2}

    def get_grants(self, r: ResourceRef) -> dict[str, set[str]]:
        """``SHOW GRANTS ON <obj>`` returns one row per (principal, privilege).
        We aggregate into the {principal: {privileges}} shape the reconciler expects.
        """
        obj_type, qualified = _object_type_and_qualified(r)
        rows = self._run(f"SHOW GRANTS ON {obj_type} {qualified}")
        grants: dict[str, set[str]] = {}
        for row in rows:
            if not row or len(row) < 2:
                continue
            # SHOW GRANTS returns columns: Principal, ActionType, ObjectType, ObjectKey
            principal, action_type = row[0], row[1]
            if not principal or not action_type:
                continue
            grants.setdefault(principal, set()).add(action_type)
        return grants

    def get_policies(self) -> set[str]:
        return self.abac.list()


# --------------------------- SDK wrapper ----------------------------------


def make_sdk_sql_runner(workspace_client: object, warehouse_id: str) -> SQLRunner:
    """Adapter from a databricks-sdk WorkspaceClient + warehouse to a SQLRunner.

    Kept as a small free function so the bulk of ``DatabricksUCClient`` stays
    SDK-agnostic and unit-testable.

    ``workspace_client`` is typed ``object`` to avoid an import-time hard
    dependency on databricks-sdk for code paths that don't use this adapter.
    Callers should pass ``databricks.sdk.WorkspaceClient()``.
    """

    def run(statement: str) -> list[list[str]]:
        # Imported lazily so unit tests that never call this don't need the SDK.
        from databricks.sdk.service.sql import StatementState  # type: ignore

        response = workspace_client.statement_execution.execute_statement(  # type: ignore[attr-defined]
            warehouse_id=warehouse_id,
            statement=statement,
            wait_timeout="30s",
        )
        if response.status is not None and response.status.state != StatementState.SUCCEEDED:
            error_msg = response.status.error.message if response.status.error else "unknown"
            raise RuntimeError(f"SQL failed ({response.status.state}): {error_msg}\n{statement}")

        if response.result is None or response.result.data_array is None:
            return []
        return [list(row) for row in response.result.data_array]

    return run
