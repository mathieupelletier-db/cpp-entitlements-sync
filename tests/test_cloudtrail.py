"""Tests for CloudTrail -> LFEvent parsing."""
import json
from pathlib import Path

import pytest

from entitlements_sync.cloudtrail import parse_event
from entitlements_sync.models import LFEventKind, LFTagAssignment, PrincipalKind

FIXTURES = Path(__file__).parent / "fixtures" / "lf_events"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_grant_permissions():
    ev = parse_event(_load("grant_permissions.json"))
    assert ev.kind is LFEventKind.GRANT_PERMISSIONS
    assert ev.resource is not None
    assert ev.resource.qualified_name == "123456789012.finance.trades"
    assert ev.principal is not None
    assert ev.principal.kind is PrincipalKind.IDP_GROUP
    assert ev.principal.identifier == "data-analysts"
    assert ev.permissions == ("SELECT",)


def test_parse_revoke_permissions():
    ev = parse_event(_load("revoke_permissions.json"))
    assert ev.kind is LFEventKind.REVOKE_PERMISSIONS


def test_parse_add_lftags_to_resource():
    ev = parse_event(_load("add_lftags_to_resource.json"))
    assert ev.kind is LFEventKind.ADD_LFTAGS_TO_RESOURCE
    assert ev.lf_tags == (LFTagAssignment("classification", "confidential"),)


def test_parse_remove_lftags_from_resource():
    ev = parse_event(_load("remove_lftags_from_resource.json"))
    assert ev.kind is LFEventKind.REMOVE_LFTAGS_FROM_RESOURCE


def test_parse_create_lf_tag():
    ev = parse_event(_load("create_lf_tag.json"))
    assert ev.kind is LFEventKind.CREATE_LF_TAG
    assert ev.lf_tags is not None
    assert ev.lf_tags[0].key == "classification"
    assert ev.resource is None
    assert ev.principal is None


def test_parse_delete_lf_tag():
    ev = parse_event(_load("delete_lf_tag.json"))
    assert ev.kind is LFEventKind.DELETE_LF_TAG


def test_parse_unknown_event_raises():
    with pytest.raises(ValueError):
        parse_event({
            "eventID": "x", "eventTime": "2026-05-06T00:00:00Z",
            "eventSource": "lakeformation.amazonaws.com", "eventName": "Mystery",
            "requestParameters": {},
        })
