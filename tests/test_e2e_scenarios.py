"""End-to-end tests covering demo scenarios 1-3 from the design spec."""
import json
from pathlib import Path

import pytest

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.cloudtrail import parse_event
from entitlements_sync.identity import IdentityResolver
from entitlements_sync.models import ResourceRef
from entitlements_sync.orchestrator import SyncOrchestrator
from entitlements_sync.translators.abac_policy import ABACPolicyTranslator
from entitlements_sync.translators.grant import GrantTranslator
from entitlements_sync.translators.tag import TagTranslator
from entitlements_sync.uc_client import InMemoryUCClient

FIXTURES = Path(__file__).parent / "fixtures" / "lf_events"
NAMESPACE = {"classification": "data_classification", "lob": "line_of_business"}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def orch():
    fixture = Path(__file__).parent / "fixtures" / "identity_mapping.json"
    return SyncOrchestrator(
        uc=InMemoryUCClient(),
        audit=InMemoryAuditSink(),
        identity=IdentityResolver.from_file(fixture),
        tag_translator=TagTranslator(namespace_map=NAMESPACE),
        abac_translator=ABACPolicyTranslator(namespace_map=NAMESPACE),
        grant_translator=GrantTranslator(),
    )


def test_scenario_1_tag_driven_inheritance(orch):
    """Spec scenario 1: tag a table classification=confidential -> UC tag + managed marker.
       Plus, the upstream CreateLFTag event creates the policy."""
    orch.handle(parse_event(_load("create_lf_tag.json")))
    orch.handle(parse_event(_load("add_lftags_to_resource.json")))

    r = ResourceRef("123456789012", "finance", "trades", None)
    assert orch.uc.get_tags(r) == {
        "data_classification": "confidential",
        "managed_by": "lf_sync",
    }
    assert "lf_sync__data_classification" in orch.uc.get_policies()
    statuses = [row.status for row in orch.audit.rows]
    assert all(s == "ok" for s in statuses), statuses


def test_scenario_2_group_grant(orch):
    """Spec scenario 2: grant IdP group SELECT -> UC GRANT recorded."""
    orch.handle(parse_event(_load("grant_permissions.json")))
    r = ResourceRef("123456789012", "finance", "trades", None)
    assert orch.uc.get_grants(r) == {"data-analysts": {"SELECT"}}


def test_scenario_3_revoke(orch):
    """Spec scenario 3: revoke -> UC REVOKE flows through, leaving no grant."""
    orch.handle(parse_event(_load("grant_permissions.json")))
    orch.handle(parse_event(_load("revoke_permissions.json")))
    r = ResourceRef("123456789012", "finance", "trades", None)
    assert orch.uc.get_grants(r) == {}


def test_audit_records_event_id_provenance(orch):
    orch.handle(parse_event(_load("grant_permissions.json")))
    assert orch.audit.rows[0].source_event_id == "ct-grant-1"
