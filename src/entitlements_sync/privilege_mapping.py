"""Level-aware translation from Lake Formation permissions to UC privileges.

LF and UC do not have isomorphic verb sets, and the mapping is *resource-level
dependent*. The most surprising case (uncovered in a real CPP run): LF
``DESCRIBE`` works at every level, but UC's ``USE CATALOG`` / ``USE SCHEMA`` are
the level-appropriate equivalents and there is no table-level "describe"
privilege — SELECT implies metadata access on a table.

This module is the single source of truth for the privilege translation. Both
the per-event grant translator and the reconciler target builder go through it.
"""
from __future__ import annotations

from .models import ResourceRef


def _resource_level(r: ResourceRef) -> str:
    """Return the UC object level a ResourceRef addresses."""
    if r.column is not None:
        return "COLUMN"
    if r.table is not None:
        return "TABLE"
    if r.database:
        return "SCHEMA"
    return "CATALOG"


# (lf_permission, resource_level) -> uc_privilege OR None to indicate the
# permission has no grantable UC equivalent at this level (so the grant is
# dropped and counted as unsupported in the audit).
_TABLE: dict[tuple[str, str], str | None] = {
    # SELECT
    ("SELECT", "TABLE"): "SELECT",
    ("SELECT", "SCHEMA"): None,         # not meaningful at this level
    ("SELECT", "CATALOG"): None,

    # DESCRIBE -> USE at parent levels; dropped at table (SELECT implies it)
    ("DESCRIBE", "CATALOG"): "USE CATALOG",
    ("DESCRIBE", "SCHEMA"): "USE SCHEMA",
    ("DESCRIBE", "TABLE"): None,

    # Writes fold to MODIFY at the table level; not grantable above
    ("ALTER", "TABLE"): "MODIFY",
    ("INSERT", "TABLE"): "MODIFY",
    ("DELETE", "TABLE"): "MODIFY",

    # DROP requires UC ownership; no grantable equivalent at any level
    ("DROP", "TABLE"): None,
    ("DROP", "SCHEMA"): None,
    ("DROP", "CATALOG"): None,

    # ALL -> ALL PRIVILEGES at every level
    ("ALL", "TABLE"): "ALL PRIVILEGES",
    ("ALL", "SCHEMA"): "ALL PRIVILEGES",
    ("ALL", "CATALOG"): "ALL PRIVILEGES",

    # Creates
    ("CREATE_TABLE", "SCHEMA"): "CREATE TABLE",
    ("CREATE_TABLE", "CATALOG"): None,
    ("CREATE_DATABASE", "CATALOG"): "CREATE SCHEMA",
}


def map_lf_to_uc_privileges(
    lf_permissions,
    resource: ResourceRef,
) -> tuple[set[str], list[str]]:
    """Translate a set of LF permissions to UC privileges for ``resource``.

    Returns ``(uc_privileges, unsupported_lf_permissions)``. ``unsupported``
    includes both LF permissions that are explicitly marked as having no UC
    equivalent at this level AND LF permissions we don't recognize at all.
    """
    level = _resource_level(resource)
    uc: set[str] = set()
    unsupported: list[str] = []
    for lf in lf_permissions:
        key = (lf, level)
        if key in _TABLE:
            mapped = _TABLE[key]
            if mapped is None:
                unsupported.append(lf)
            else:
                uc.add(mapped)
        else:
            unsupported.append(lf)
    return uc, unsupported
