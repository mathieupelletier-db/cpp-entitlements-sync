"""Tests for the UC client abstraction and in-memory fake."""
import pytest

from entitlements_sync.models import (
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOp,
    SyncOpKind,
)
from entitlements_sync.uc_client import InMemoryUCClient


@pytest.fixture
def client():
    return InMemoryUCClient()


def _set_tag(resource, key, value):
    return SyncOp(
        kind=SyncOpKind.SET_TAG,
        resource=resource,
        principal=None,
        permissions=(),
        tag_key=key,
        tag_value=value,
        policy_name=None,
    )


def _unset_tag(resource, key):
    return SyncOp(
        kind=SyncOpKind.UNSET_TAG,
        resource=resource,
        principal=None,
        permissions=(),
        tag_key=key,
        tag_value=None,
        policy_name=None,
    )


def _grant(resource, principal, perms):
    return SyncOp(
        kind=SyncOpKind.GRANT,
        resource=resource,
        principal=principal,
        permissions=perms,
        tag_key=None,
        tag_value=None,
        policy_name=None,
    )


def _revoke(resource, principal, perms):
    return SyncOp(
        kind=SyncOpKind.REVOKE,
        resource=resource,
        principal=principal,
        permissions=perms,
        tag_key=None,
        tag_value=None,
        policy_name=None,
    )


def _upsert_policy(name):
    return SyncOp(
        kind=SyncOpKind.UPSERT_POLICY,
        resource=None,
        principal=None,
        permissions=(),
        tag_key=None,
        tag_value=None,
        policy_name=name,
    )


def _delete_policy(name):
    return SyncOp(
        kind=SyncOpKind.DELETE_POLICY,
        resource=None,
        principal=None,
        permissions=(),
        tag_key=None,
        tag_value=None,
        policy_name=name,
    )


def test_set_tag(client):
    r = ResourceRef("prod", "finance", "trades", None)
    client.apply(_set_tag(r, "data_classification", "confidential"))
    assert client.get_tags(r) == {"data_classification": "confidential"}


def test_unset_tag(client):
    r = ResourceRef("prod", "finance", "trades", None)
    client.apply(_set_tag(r, "data_classification", "confidential"))
    client.apply(_unset_tag(r, "data_classification"))
    assert client.get_tags(r) == {}


def test_grant_and_revoke(client):
    r = ResourceRef("prod", "finance", "trades", None)
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts")
    client.apply(_grant(r, p, ("SELECT",)))
    assert client.get_grants(r) == {"data-analysts": {"SELECT"}}
    client.apply(_revoke(r, p, ("SELECT",)))
    assert client.get_grants(r) == {}


def test_grant_is_idempotent(client):
    r = ResourceRef("prod", "finance", "trades", None)
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts")
    op = _grant(r, p, ("SELECT",))
    client.apply(op)
    client.apply(op)  # second apply must not error or duplicate
    assert client.get_grants(r) == {"data-analysts": {"SELECT"}}


def test_upsert_and_delete_policy(client):
    client.apply(_upsert_policy("deny_confidential_to_public"))
    assert "deny_confidential_to_public" in client.get_policies()
    client.apply(_delete_policy("deny_confidential_to_public"))
    assert "deny_confidential_to_public" not in client.get_policies()
