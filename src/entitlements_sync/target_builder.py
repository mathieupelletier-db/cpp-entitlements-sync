"""Build a TargetUCState from a snapshot of Lake Formation state.

This module is the heart of the reconciler-only design: it walks LF via an
``LFReader``, resolves principals via the ``IdentityResolver``, applies the
LF-Tag → UC-tag namespace map, and materializes one UC ABAC policy per LF-Tag
key. The output is a ``TargetUCState`` that the ``Reconciler`` diffs against
the live UC state.

Translation rules mirror the per-event translators
(``translators/{tag,abac_policy,grant}.py``) — kept in sync by tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .identity import IdentityResolver
from .lf_reader import LFReader
from .models import ResourceRef
from .privilege_mapping import map_lf_to_uc_privileges
from .reconciler import TargetUCState
from .translators.abac_policy import POLICY_NAME_PREFIX

log = logging.getLogger(__name__)


@dataclass
class BuildReport:
    """Counters surfaced for ops dashboards and the audit log."""
    identity_unresolved: int = 0
    unsupported_permissions: int = 0


def build_target_state(
    *,
    reader: LFReader,
    resolver: IdentityResolver,
    tag_namespace_map: dict[str, str],
) -> tuple[TargetUCState, BuildReport]:
    """Construct the desired UC state from the current LF snapshot."""
    report = BuildReport()

    target_tags: dict[ResourceRef, dict[str, str]] = {}
    target_grants: dict[ResourceRef, dict[str, set[str]]] = {}
    managed: set[ResourceRef] = set()

    def uc_tag_key(lf_key: str) -> str:
        return tag_namespace_map.get(lf_key, lf_key)

    for resource in reader.list_in_scope_resources():
        log.info("scanning %s", resource.qualified_name)
        # Tags: mirror with namespace remap. Empty list still marks the resource managed.
        lf_tags = reader.get_lf_tags_on_resource(resource)
        if lf_tags:
            target_tags[resource] = {uc_tag_key(t.key): t.value for t in lf_tags}
            managed.add(resource)

        # Grants: resolve each principal, drop unsupported privileges, merge into per-resource map.
        lf_grants = reader.list_grants_on_resource(resource)
        if not lf_grants:
            continue

        managed.add(resource)
        per_resource: dict[str, set[str]] = target_grants.setdefault(resource, {})

        for grant in lf_grants:
            resolution = resolver.resolve(grant.principal)
            if resolution.status != "ok" or resolution.principal is None:
                report.identity_unresolved += 1
                log.info(
                    "  identity unresolved: %s/%s — %s",
                    grant.principal.kind.value,
                    grant.principal.identifier,
                    resolution.note,
                )
                continue

            uc_perms, unsupported = map_lf_to_uc_privileges(grant.permissions, resource)
            report.unsupported_permissions += len(unsupported)
            if unsupported:
                log.info(
                    "  unsupported perms %s dropped for %s on %s",
                    sorted(unsupported),
                    resolution.principal.identifier,
                    resource.qualified_name,
                )

            if not uc_perms:
                continue

            per_resource.setdefault(resolution.principal.identifier, set()).update(uc_perms)

        # If every grant on this resource was unresolvable, drop the empty bucket.
        if not per_resource:
            target_grants.pop(resource, None)

    # Policies: one per LF-Tag key in the dictionary, namespace-remapped.
    target_policies = {
        f"{POLICY_NAME_PREFIX}{uc_tag_key(k)}" for k in reader.list_lf_tag_keys()
    }

    # Synthesize parent USE CATALOG / USE SCHEMA grants so the UC privilege
    # chain actually works for downstream queries. These are additive-only —
    # we MUST NOT revert other principals' USE grants on the catalog/schema.
    additive_grants = _derive_additive_use_grants(target_grants)

    return (
        TargetUCState(
            tags=target_tags,
            grants=target_grants,
            policies=target_policies,
            managed_resources=managed,
            additive_grants=additive_grants,
        ),
        report,
    )


def _derive_additive_use_grants(
    target_grants: dict[ResourceRef, dict[str, set[str]]],
) -> dict[ResourceRef, dict[str, set[str]]]:
    """For each (resource, principal) in ``target_grants``, emit the parent
    USE CATALOG / USE SCHEMA grants the principal needs for the chain to work.

    A grant on a TABLE implies the principal needs USE SCHEMA on the parent
    schema and USE CATALOG on the parent catalog. A grant on a SCHEMA implies
    they need USE CATALOG on the parent catalog. Grants directly on a CATALOG
    need no parent. The output groups everything by parent ResourceRef so the
    reconciler can iterate and emit GRANTs in one pass.
    """
    parents: dict[ResourceRef, dict[str, set[str]]] = {}

    def add(parent: ResourceRef, principal_id: str, priv: str) -> None:
        parents.setdefault(parent, {}).setdefault(principal_id, set()).add(priv)

    for resource, principals in target_grants.items():
        if not principals:
            continue
        catalog_ref = ResourceRef(
            catalog=resource.catalog, database="", table=None, column=None
        )
        schema_ref = (
            ResourceRef(
                catalog=resource.catalog,
                database=resource.database,
                table=None,
                column=None,
            )
            if resource.database
            else None
        )
        for principal_id in principals:
            if resource.table is not None and schema_ref is not None:
                add(schema_ref, principal_id, "USE SCHEMA")
                add(catalog_ref, principal_id, "USE CATALOG")
            elif resource.database:
                add(catalog_ref, principal_id, "USE CATALOG")
            # If the grant is directly on the catalog, no parent is needed.

    return parents
