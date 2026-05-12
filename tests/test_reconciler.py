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


def test_additive_grants_apply_when_missing(fresh_uc, audit):
    """USE CATALOG on the catalog is granted to the listed principal."""
    catalog_ref = ResourceRef("cpp_lake", "", None, None)
    target = TargetUCState(
        tags={}, grants={}, policies=set(), managed_resources=set(),
        additive_grants={catalog_ref: {"analysts": {"USE CATALOG"}}},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert fresh_uc.get_grants(catalog_ref) == {"analysts": {"USE CATALOG"}}
    assert any(row.notes == "reconciler:missing-additive" for row in audit.rows)
    assert report.missing_ops >= 1


def test_additive_grants_never_revert_other_principals(fresh_uc, audit):
    """The critical safety property: other principals' USE CATALOG grants on
    the same catalog must NOT be touched. The reconciler doesn't own the catalog."""
    catalog_ref = ResourceRef("cpp_lake", "", None, None)
    # Pre-populate UC with an unrelated principal's USE CATALOG grant
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=catalog_ref,
        principal=Principal(PrincipalKind.IDP_GROUP, "stranger"),
        permissions=("USE CATALOG",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    target = TargetUCState(
        tags={}, grants={}, policies=set(), managed_resources=set(),
        additive_grants={catalog_ref: {"analysts": {"USE CATALOG"}}},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)

    grants = fresh_uc.get_grants(catalog_ref)
    assert grants["stranger"] == {"USE CATALOG"}        # untouched
    assert grants["analysts"] == {"USE CATALOG"}        # added
    assert report.drift_ops == 0                          # no revokes were issued


def test_additive_grant_already_present_is_noop(fresh_uc, audit):
    catalog_ref = ResourceRef("cpp_lake", "", None, None)
    fresh_uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=catalog_ref,
        principal=Principal(PrincipalKind.IDP_GROUP, "analysts"),
        permissions=("USE CATALOG",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    target = TargetUCState(
        tags={}, grants={}, policies=set(), managed_resources=set(),
        additive_grants={catalog_ref: {"analysts": {"USE CATALOG"}}},
    )
    rec = Reconciler(uc=fresh_uc, audit=audit)
    report = rec.reconcile(target)
    assert report.missing_ops == 0
    assert report.drift_ops == 0


def test_uc_apply_failure_is_caught_and_audited_as_error(audit):
    """A failing op (e.g., UC tag-policy rejects the value) must NOT abort the
    run. The error is captured in the audit row; the reconciler continues."""
    r = _trades()
    r2 = ResourceRef("123456789012", "finance", "positions", None)

    class FlakyUC:
        def __init__(self):
            self.applied = []
        def apply(self, op):
            # Fail on the first SET_TAG, succeed on subsequent ops
            if op.kind is SyncOpKind.SET_TAG and not self.applied:
                self.applied.append(op)
                raise RuntimeError("Tag value 'internal' not allowed by tag policy")
            self.applied.append(op)
        def get_tags(self, rr):
            return {}
        def get_grants(self, rr):
            return {}
        def get_policies(self):
            return set()

    uc = FlakyUC()
    target = TargetUCState(
        tags={r: {"data_classification": "internal"}, r2: {"data_classification": "public"}},
        grants={},
        policies=set(),
        managed_resources={r, r2},
    )
    rec = Reconciler(uc=uc, audit=audit)
    report = rec.reconcile(target)

    # First op failed; subsequent ops still happened
    assert len(uc.applied) >= 3  # 2 tag ops on r (failed first one + managed_by), and ops on r2
    assert report.audit_rows_written >= 3

    # At least one audit row records the failure with the error string
    errors = [row for row in audit.rows if row.status == "error"]
    assert len(errors) == 1
    assert "not allowed by tag policy" in errors[0].error
