"""Tests for the Reconciler: diff target UC state vs current UC, emit corrective ops with drift audit."""
import pytest

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.models import (
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOp,
    SyncOpKind,
)
from entitlements_sync.reconciler import Reconciler, TargetUCState
from entitlements_sync.uc_client import InMemoryUCClient


@pytest.fixture
def fresh_uc():
    return InMemoryUCClient()


@pytest.fixture
def audit():
    return InMemoryAuditSink()


def _trades() -> ResourceRef:
    return ResourceRef("123456789012", "finance", "trades", None)


def test_missing_tag_is_added(fresh_uc, audit):
    r = _trades()
    target = TargetUCState(
        tags={r: {"data_classification": "confidential"}},
        grants={},
        policies=set(),
        managed_resources={r},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert fresh_uc.get_tags(r) == {"data_classification": "confidential", "managed_by": "lf_sync"}
    assert report.missing_ops == 2  # the data tag + the managed_by marker
    assert any(row.notes == "reconciler:missing" for row in audit.rows)


def test_missing_grant_is_added(fresh_uc, audit):
    r = _trades()
    target = TargetUCState(
        tags={},
        grants={r: {"data-analysts": {"SELECT"}}},
        policies=set(),
        managed_resources={r},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert fresh_uc.get_grants(r) == {"data-analysts": {"SELECT"}}
    assert report.missing_ops >= 1


def test_missing_policy_is_added(fresh_uc, audit):
    target = TargetUCState(
        tags={},
        grants={},
        policies={"lf_sync__data_classification"},
        managed_resources=set(),
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    rec.reconcile(target)
    assert "lf_sync__data_classification" in fresh_uc.get_policies()


def test_drift_tag_is_reverted_on_managed_resource(fresh_uc, audit):
    r = _trades()
    # Pre-populate UC with a tag that's not in the target -> drift
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="data_classification", tag_value="public", policy_name=None,
    ))
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="managed_by", tag_value="lf_sync", policy_name=None,
    ))
    target = TargetUCState(
        tags={r: {}},  # target says: no data tags (managed_by always implicit)
        grants={},
        policies=set(),
        managed_resources={r},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    # data_classification reverted; managed_by stays
    assert fresh_uc.get_tags(r) == {"managed_by": "lf_sync"}
    assert report.drift_ops >= 1
    assert any(row.notes == "reconciler:drift" for row in audit.rows)


def test_drift_grant_is_reverted(fresh_uc, audit):
    r = _trades()
    p = Principal(PrincipalKind.IDP_GROUP, "ghosts")
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=r, principal=p, permissions=("SELECT",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    target = TargetUCState(
        tags={},
        grants={r: {}},  # target says: no grants on this resource
        policies=set(),
        managed_resources={r},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert fresh_uc.get_grants(r) == {}
    assert report.drift_ops >= 1


def test_drift_policy_is_reverted(fresh_uc, audit):
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.UPSERT_POLICY, resource=None, principal=None, permissions=(),
        tag_key=None, tag_value=None, policy_name="orphan_policy",
    ))
    target = TargetUCState(
        tags={}, grants={}, policies=set(), managed_resources=set(),
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert fresh_uc.get_policies() == set()
    assert report.drift_ops >= 1


def test_unmanaged_resource_drift_left_alone(fresh_uc, audit):
    r = _trades()
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts")
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=r, principal=p, permissions=("SELECT",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="data_classification", tag_value="public", policy_name=None,
    ))
    # Resource NOT in managed_resources
    target = TargetUCState(tags={}, grants={}, policies=set(), managed_resources=set())
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    # untouched
    assert fresh_uc.get_grants(r) == {"data-analysts": {"SELECT"}}
    assert fresh_uc.get_tags(r) == {"data_classification": "public"}
    assert report.drift_ops == 0
    assert report.missing_ops == 0


def test_in_sync_state_is_a_noop(fresh_uc, audit):
    r = _trades()
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="data_classification", tag_value="confidential", policy_name=None,
    ))
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="managed_by", tag_value="lf_sync", policy_name=None,
    ))
    target = TargetUCState(
        tags={r: {"data_classification": "confidential"}},
        grants={},
        policies=set(),
        managed_resources={r},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert report.missing_ops == 0
    assert report.drift_ops == 0
