"""Tests for SyncOrchestrator: end-to-end event -> UC state + audit."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.identity import IdentityResolver
from entitlements_sync.models import (
    LFEvent,
    LFEventKind,
    LFTagAssignment,
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOpKind,
)
from entitlements_sync.orchestrator import SyncOrchestrator
from entitlements_sync.translators.abac_policy import ABACPolicyTranslator
from entitlements_sync.translators.grant import GrantTranslator
from entitlements_sync.translators.tag import TagTranslator
from entitlements_sync.uc_client import InMemoryUCClient


@pytest.fixture
def orchestrator():
    fixture = Path(__file__).parent / "fixtures" / "identity_mapping.json"
    return SyncOrchestrator(
        uc=InMemoryUCClient(),
        audit=InMemoryAuditSink(),
        identity=IdentityResolver.from_file(fixture),
        tag_translator=TagTranslator(namespace_map={"classification": "data_classification"}),
        abac_translator=ABACPolicyTranslator(namespace_map={"classification": "data_classification"}),
        grant_translator=GrantTranslator(),
    )


def _grant_ev(principal: Principal) -> LFEvent:
    return LFEvent(
        kind=LFEventKind.GRANT_PERMISSIONS,
        event_id="evt-grant",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=principal,
        permissions=("SELECT",),
        lf_tags=None,
    )


def test_grant_to_idp_group_applies_to_uc_and_writes_audit(orchestrator):
    p = Principal(PrincipalKind.IDP_GROUP, "data-analysts")
    orchestrator.handle(_grant_ev(p))
    r = ResourceRef("prod", "finance", "trades", None)
    assert orchestrator.uc.get_grants(r) == {"data-analysts": {"SELECT"}}
    rows = orchestrator.audit.rows
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].op_kind is SyncOpKind.GRANT


def test_iam_role_resolved_via_override(orchestrator):
    p = Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123456789012:role/AnalystRole")
    orchestrator.handle(_grant_ev(p))
    r = ResourceRef("prod", "finance", "trades", None)
    # override resolves to data-analysts group
    assert orchestrator.uc.get_grants(r) == {"data-analysts": {"SELECT"}}


def test_unresolved_iam_role_skips_apply_and_audits_unresolved(orchestrator):
    p = Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123456789012:role/UnknownRole")
    orchestrator.handle(_grant_ev(p))
    r = ResourceRef("prod", "finance", "trades", None)
    assert orchestrator.uc.get_grants(r) == {}
    rows = orchestrator.audit.rows
    assert len(rows) == 1
    assert rows[0].status == "identity_unresolved"


def test_add_lftag_event_sets_uc_tag_and_managed_marker(orchestrator):
    ev = LFEvent(
        kind=LFEventKind.ADD_LFTAGS_TO_RESOURCE,
        event_id="evt-tag",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=None,
        permissions=(),
        lf_tags=(LFTagAssignment("classification", "confidential"),),
    )
    orchestrator.handle(ev)
    r = ResourceRef("prod", "finance", "trades", None)
    assert orchestrator.uc.get_tags(r) == {
        "data_classification": "confidential",
        "managed_by": "lf_sync",
    }


def test_create_lf_tag_event_upserts_policy(orchestrator):
    ev = LFEvent(
        kind=LFEventKind.CREATE_LF_TAG,
        event_id="evt-create-tag",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=None,
        principal=None,
        permissions=(),
        lf_tags=(LFTagAssignment("classification", ""),),
    )
    orchestrator.handle(ev)
    assert "lf_sync__data_classification" in orchestrator.uc.get_policies()


def test_unsupported_permissions_audited_as_unsupported(orchestrator):
    ev = LFEvent(
        kind=LFEventKind.GRANT_PERMISSIONS,
        event_id="evt-unsupported",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=Principal(PrincipalKind.IDP_GROUP, "data-analysts"),
        permissions=("ASSOCIATE",),
        lf_tags=None,
    )
    orchestrator.handle(ev)
    rows = orchestrator.audit.rows
    assert any(r.status == "unsupported" for r in rows)


def test_unresolved_iam_role_audit_row_uses_none_op_kind(orchestrator):
    """Non-op audit rows must use SyncOpKind.NONE so they don't masquerade as grants."""
    p = Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123456789012:role/UnknownRole")
    orchestrator.handle(_grant_ev(p))
    assert orchestrator.audit.rows[0].op_kind is SyncOpKind.NONE
