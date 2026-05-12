"""Tests for the IdentityResolver."""
from pathlib import Path

import pytest

from entitlements_sync.identity import IdentityResolution, IdentityResolver  # noqa: F401
from entitlements_sync.models import Principal, PrincipalKind


@pytest.fixture
def resolver() -> IdentityResolver:
    fixture = Path(__file__).parent / "fixtures" / "identity_mapping.json"
    return IdentityResolver.from_file(fixture)


def test_idp_group_passthrough(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts")
    res = resolver.resolve(p)
    assert res.status == "ok"
    assert res.principal == p


def test_idp_group_rename(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts-old")
    res = resolver.resolve(p)
    assert res.status == "ok"
    assert res.principal == Principal(PrincipalKind.IDP_GROUP, "data-analysts")


def test_idp_user_passthrough(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IDP_USER, "alice@cpp.example")
    res = resolver.resolve(p)
    assert res.status == "ok"
    assert res.principal == p


def test_iam_role_with_override(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123456789012:role/AnalystRole")
    res = resolver.resolve(p)
    assert res.status == "ok"
    assert res.principal == Principal(PrincipalKind.IDP_GROUP, "data-analysts")


def test_iam_role_without_override(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123456789012:role/UnknownRole")
    res = resolver.resolve(p)
    assert res.status == "identity_unresolved"
    assert res.principal is None


def test_iam_user_with_override(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IAM_USER, "demo/pension-ron")
    res = resolver.resolve(p)
    assert res.status == "ok"
    assert res.principal == Principal(PrincipalKind.IDP_USER, "ron.smith@cpp.example.com")


def test_iam_user_without_override(resolver: IdentityResolver):
    p = Principal(PrincipalKind.IAM_USER, "jane.doe")
    res = resolver.resolve(p)
    assert res.status == "identity_unresolved"
    assert res.principal is None
    assert "IAM user" in res.note
