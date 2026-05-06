"""UC Client abstraction. The real implementation (Databricks SDK) is in Plan 2."""
from __future__ import annotations

from typing import Protocol

from .models import ResourceRef, SyncOp, SyncOpKind


class UCClient(Protocol):
    def apply(self, op: SyncOp) -> None:
        ...


class InMemoryUCClient:
    """In-memory fake. Holds tags-per-resource, grants-per-resource, and named policies."""

    def __init__(self) -> None:
        self._tags: dict[str, dict[str, str]] = {}            # qualified_name -> {tag_key: tag_value}
        self._grants: dict[str, dict[str, set[str]]] = {}     # qualified_name -> {principal_id: {permissions}}
        self._policies: set[str] = set()                       # policy_name set

    def apply(self, op: SyncOp) -> None:
        if op.kind is SyncOpKind.SET_TAG:
            self._set_tag(op)
        elif op.kind is SyncOpKind.UNSET_TAG:
            self._unset_tag(op)
        elif op.kind is SyncOpKind.GRANT:
            self._grant(op)
        elif op.kind is SyncOpKind.REVOKE:
            self._revoke(op)
        elif op.kind is SyncOpKind.UPSERT_POLICY:
            assert op.policy_name is not None
            self._policies.add(op.policy_name)
        elif op.kind is SyncOpKind.DELETE_POLICY:
            assert op.policy_name is not None
            self._policies.discard(op.policy_name)
        else:
            raise ValueError(f"Unknown SyncOpKind: {op.kind}")

    def _set_tag(self, op: SyncOp) -> None:
        assert op.resource is not None and op.tag_key is not None and op.tag_value is not None
        self._tags.setdefault(op.resource.qualified_name, {})[op.tag_key] = op.tag_value

    def _unset_tag(self, op: SyncOp) -> None:
        assert op.resource is not None and op.tag_key is not None
        bucket = self._tags.get(op.resource.qualified_name)
        if bucket is not None:
            bucket.pop(op.tag_key, None)
            if not bucket:
                self._tags.pop(op.resource.qualified_name, None)

    def _grant(self, op: SyncOp) -> None:
        assert op.resource is not None and op.principal is not None
        bucket = self._grants.setdefault(op.resource.qualified_name, {})
        perms = bucket.setdefault(op.principal.identifier, set())
        perms.update(op.permissions)

    def _revoke(self, op: SyncOp) -> None:
        assert op.resource is not None and op.principal is not None
        bucket = self._grants.get(op.resource.qualified_name)
        if bucket is None:
            return
        perms = bucket.get(op.principal.identifier)
        if perms is None:
            return
        perms.difference_update(op.permissions)
        if not perms:
            bucket.pop(op.principal.identifier, None)
        if not bucket:
            self._grants.pop(op.resource.qualified_name, None)

    # --- inspection helpers used by tests ---

    def get_tags(self, r: ResourceRef) -> dict[str, str]:
        return dict(self._tags.get(r.qualified_name, {}))

    def get_grants(self, r: ResourceRef) -> dict[str, set[str]]:
        return {p: set(perms) for p, perms in self._grants.get(r.qualified_name, {}).items()}

    def get_policies(self) -> set[str]:
        return set(self._policies)
