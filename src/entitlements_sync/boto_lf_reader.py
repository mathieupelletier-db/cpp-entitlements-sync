"""boto3-backed LFReader.

Wraps the AWS Lake Formation client. Scope is config-driven: the operator
passes the catalog id and the list of in-scope ``ResourceRef``s at construction
time. Auto-discovery via Glue can be layered on top later — see
``scripts/discover_scope.py`` (TODO).

Principal parsing is pluggable. The default heuristic handles the three shapes
seen at CPP: IAM roles, IDC group ARNs, and email/UPN identifiers. Customers
whose principal ARNs follow a different convention should inject a custom
``principal_parser`` at construction time.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .lf_reader import LFGrant
from .models import LFTagAssignment, Principal, PrincipalKind, ResourceRef

log = logging.getLogger(__name__)


PrincipalParser = Callable[[str], Principal]


def default_principal_parser(identifier: str) -> Principal:
    """Heuristic mapping from LF ``DataLakePrincipalIdentifier`` to ``Principal``.

    Order matters; the first matching rule wins. IAM users have no direct UC
    equivalent — they emerge as ``IAM_USER`` and require an explicit
    ``iam_user_overrides`` entry in the IdentityResolver.
    """
    if identifier.startswith("arn:aws:iam:") and ":role/" in identifier:
        role_name = identifier.split(":role/", 1)[1]
        return Principal(PrincipalKind.IAM_ROLE, role_name)
    if identifier.startswith("arn:aws:iam:") and ":user/" in identifier:
        # IAM user; everything after :user/ is the user name (may include path
        # segments like demo/ron-sandbox). Used as the override lookup key.
        user_name = identifier.split(":user/", 1)[1]
        return Principal(PrincipalKind.IAM_USER, user_name)
    if identifier.startswith("arn:aws:identitystore:") or identifier.startswith("arn:aws:sso:"):
        # IDC group ARN — the display name is not in the ARN; the operator must
        # ensure the IdentityResolver knows how to map this. We use the trailing
        # segment as a stable identifier; group_renames can fix up display names.
        return Principal(PrincipalKind.IDP_GROUP, identifier.rsplit("/", 1)[-1])
    if "@" in identifier:
        return Principal(PrincipalKind.IDP_USER, identifier)
    # Bare group display name (common in CPP test setups)
    return Principal(PrincipalKind.IDP_GROUP, identifier)


@dataclass
class BotoLFReader:
    """LF reader backed by a boto3 LakeFormation client.

    The LakeFormation client is injected so tests can stub it via
    ``botocore.stub.Stubber``. An optional Glue client enables auto-expansion
    of database-level entries: when the operator configures a bare
    ``{database: X}`` (no table), the reader transparently fans out to include
    every table in that database in addition to the database itself. The
    expansion is cached so the reconciler can call ``list_in_scope_resources``
    freely without re-hitting Glue.
    """

    client: Any  # boto3 LakeFormation client
    catalog_id: str
    in_scope_resources: list[ResourceRef]
    principal_parser: PrincipalParser = default_principal_parser
    glue_client: Any | None = None  # optional; enables database -> tables expansion
    _expanded_scope: list[ResourceRef] | None = field(default=None, init=False, repr=False)

    def list_in_scope_resources(self) -> list[ResourceRef]:
        if self._expanded_scope is not None:
            return list(self._expanded_scope)

        if self.glue_client is None:
            # No Glue client -> use the literal config. Caller is responsible
            # for enumerating tables.
            self._expanded_scope = list(self.in_scope_resources)
            return list(self._expanded_scope)

        expanded: list[ResourceRef] = []
        seen: set[ResourceRef] = set()
        for entry in self.in_scope_resources:
            if entry not in seen:
                expanded.append(entry)
                seen.add(entry)
            if entry.table is None and entry.database:
                # Database-level entry -> also include every table in it.
                tables = self._list_tables_in_database(entry.database)
                log.info(
                    "expanded database %s -> %d table(s)", entry.database, len(tables)
                )
                for table_name in tables:
                    child = ResourceRef(
                        catalog=entry.catalog,
                        database=entry.database,
                        table=table_name,
                        column=None,
                    )
                    if child not in seen:
                        expanded.append(child)
                        seen.add(child)
        log.info("in-scope resources after expansion: %d", len(expanded))
        self._expanded_scope = expanded
        return list(self._expanded_scope)

    def _list_tables_in_database(self, database: str) -> list[str]:
        """Page through ``glue.get_tables`` for the given database."""
        tables: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "CatalogId": self.catalog_id,
                "DatabaseName": database,
            }
            if next_token is not None:
                kwargs["NextToken"] = next_token
            response = self.glue_client.get_tables(**kwargs)
            for entry in response.get("TableList", []) or []:
                name = entry.get("Name")
                if name:
                    tables.append(name)
            next_token = response.get("NextToken")
            if not next_token:
                break
        return tables

    def list_lf_tag_keys(self) -> list[str]:
        keys: list[str] = []
        paginator_kwargs: dict[str, Any] = {"CatalogId": self.catalog_id}
        next_token: str | None = None
        while True:
            if next_token is not None:
                paginator_kwargs["NextToken"] = next_token
            response = self.client.list_lf_tags(**paginator_kwargs)
            for tag in response.get("LFTags", []):
                key = tag.get("TagKey")
                if key:
                    keys.append(key)
            next_token = response.get("NextToken")
            if not next_token:
                break
        return keys

    def get_lf_tags_on_resource(self, r: ResourceRef) -> list[LFTagAssignment]:
        """Return effective LF-Tags on ``r`` (directly assigned + inherited).

        ``ShowAssignedLFTags=False`` is the LF default which already returns
        inherited tags; we pass it explicitly for clarity. We do not currently
        request column-level tags — that lives in the Out-of-POC tag-on-column
        roadmap item.
        """
        log.debug("LF read: tags on %s", r.qualified_name)
        resource_payload = _build_resource_payload(self.catalog_id, r)
        response = self.client.get_resource_lf_tags(
            CatalogId=self.catalog_id,
            Resource=resource_payload,
            ShowAssignedLFTags=False,
        )
        assignments: list[LFTagAssignment] = []
        for bucket in ("LFTagOnDatabase", "LFTagsOnTable"):
            for entry in response.get(bucket, []) or []:
                key = entry.get("TagKey")
                values = entry.get("TagValues") or []
                # LF allows multi-value tags; we explode them into one assignment
                # per value so the UC mirror can faithfully reflect the set.
                for value in values:
                    if key is not None and value is not None:
                        assignments.append(LFTagAssignment(key=key, value=value))
        return assignments

    def list_grants_on_resource(self, r: ResourceRef) -> list[LFGrant]:
        log.debug("LF read: grants on %s", r.qualified_name)
        resource_payload = _build_resource_payload(self.catalog_id, r)
        # Aggregate (principal, perms) across paginated pages.
        aggregator: dict[str, set[str]] = {}
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "CatalogId": self.catalog_id,
                "Resource": resource_payload,
            }
            if next_token is not None:
                kwargs["NextToken"] = next_token
            response = self.client.list_permissions(**kwargs)
            for entry in response.get("PrincipalResourcePermissions", []) or []:
                principal_ident = (
                    entry.get("Principal", {}).get("DataLakePrincipalIdentifier")
                )
                if principal_ident is None:
                    continue
                perms = entry.get("Permissions", []) or []
                aggregator.setdefault(principal_ident, set()).update(perms)
            next_token = response.get("NextToken")
            if not next_token:
                break
        return [
            LFGrant(
                principal=self.principal_parser(ident),
                permissions=tuple(sorted(perms)),
            )
            for ident, perms in aggregator.items()
        ]


def _build_resource_payload(catalog_id: str, r: ResourceRef) -> dict[str, Any]:
    """Render a ``ResourceRef`` as the LF ``Resource`` request shape."""
    if r.table is not None:
        return {
            "Table": {
                "CatalogId": catalog_id,
                "DatabaseName": r.database,
                "Name": r.table,
            }
        }
    return {
        "Database": {
            "CatalogId": catalog_id,
            "Name": r.database,
        }
    }
