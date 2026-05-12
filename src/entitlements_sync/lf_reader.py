"""Read interface over Lake Formation.

The reconciler pulls a desired-state snapshot through this interface. The
real boto3-backed implementation lives in ``boto_lf_reader.py``; this module
defines the Protocol and an in-memory fake used in tests and the run-local CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import LFTagAssignment, Principal, ResourceRef


@dataclass(frozen=True)
class LFGrant:
    """A LF permission grant on a resource for a principal."""
    principal: Principal
    permissions: tuple[str, ...]


class LFReader(Protocol):
    """Pull desired-state snapshots from Lake Formation.

    The reader is invoked once per reconciliation pass. Implementations are
    expected to be read-only and idempotent. Reads that are eventually
    consistent are acceptable; the reconciler bounds the worst-case sync SLA.
    """

    def list_in_scope_resources(self) -> list[ResourceRef]:
        """Return every catalog/database/table currently in scope for sync."""
        ...

    def get_lf_tags_on_resource(self, r: ResourceRef) -> list[LFTagAssignment]:
        """LF-Tags directly assigned to ``r``. Implementations should NOT
        include tags inherited from parent resources — inheritance is handled
        by the target builder."""
        ...

    def list_lf_tag_keys(self) -> list[str]:
        """All LF-Tag keys defined in the LF tag dictionary. One UC ABAC policy
        is materialized per key."""
        ...

    def list_grants_on_resource(self, r: ResourceRef) -> list[LFGrant]:
        """Resource-level grants on ``r`` (the LF "permissions" surface, not
        LF-Tag-based policies). Each entry pairs a principal with the set of
        permissions held."""
        ...


@dataclass
class InMemoryLFReader:
    """Hand-built fake for tests and the run-local CLI.

    Fields are public so tests can assemble fixtures without builders. All
    collections default to empty; populate only what the test needs.
    """

    resources: list[ResourceRef] = field(default_factory=list)
    tags_by_resource: dict[ResourceRef, list[LFTagAssignment]] = field(default_factory=dict)
    grants_by_resource: dict[ResourceRef, list[LFGrant]] = field(default_factory=dict)
    tag_keys: list[str] = field(default_factory=list)

    def list_in_scope_resources(self) -> list[ResourceRef]:
        return list(self.resources)

    def get_lf_tags_on_resource(self, r: ResourceRef) -> list[LFTagAssignment]:
        return list(self.tags_by_resource.get(r, []))

    def list_lf_tag_keys(self) -> list[str]:
        return list(self.tag_keys)

    def list_grants_on_resource(self, r: ResourceRef) -> list[LFGrant]:
        return list(self.grants_by_resource.get(r, []))
