"""Resolve LF principals to UC identities."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import Principal, PrincipalKind


@dataclass(frozen=True)
class IdentityResolution:
    status: str  # "ok" | "identity_unresolved"
    principal: Optional[Principal]
    note: Optional[str]


class IdentityResolver:
    """Resolution rules (in order):
       1. IdP group -> apply group_renames; pass through.
       2. IdP user  -> pass through (assumes shared IdP federation).
       3. IAM role  -> look up iam_role_overrides; if missing, return unresolved.
    """

    def __init__(
        self,
        *,
        iam_role_overrides: dict[str, dict[str, str]],
        group_renames: dict[str, str],
    ) -> None:
        self._iam_role_overrides = iam_role_overrides
        self._group_renames = group_renames

    @classmethod
    def from_file(cls, path: Path) -> "IdentityResolver":
        data = json.loads(Path(path).read_text())
        return cls(
            iam_role_overrides=data.get("iam_role_overrides", {}),
            group_renames=data.get("group_renames", {}),
        )

    def resolve(self, p: Principal) -> IdentityResolution:
        if p.kind is PrincipalKind.IDP_GROUP:
            renamed = self._group_renames.get(p.identifier, p.identifier)
            return IdentityResolution(
                status="ok",
                principal=Principal(PrincipalKind.IDP_GROUP, renamed),
                note=None if renamed == p.identifier else f"renamed from {p.identifier}",
            )
        if p.kind is PrincipalKind.IDP_USER:
            return IdentityResolution(status="ok", principal=p, note=None)
        if p.kind is PrincipalKind.IAM_ROLE:
            override = self._iam_role_overrides.get(p.identifier)
            if override is None:
                return IdentityResolution(
                    status="identity_unresolved",
                    principal=None,
                    note=f"no override for IAM role {p.identifier}",
                )
            return IdentityResolution(
                status="ok",
                principal=Principal(
                    kind=PrincipalKind(override["kind"]),
                    identifier=override["identifier"],
                ),
                note=f"resolved via override from {p.identifier}",
            )
        raise ValueError(f"Unknown PrincipalKind: {p.kind}")
