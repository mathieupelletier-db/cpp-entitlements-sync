"""Tests for ABACPolicyTranslator: LF tag lifecycle -> UC policy upsert/delete."""
from datetime import datetime, timezone

from entitlements_sync.models import LFEvent, LFEventKind, LFTagAssignment, SyncOpKind
from entitlements_sync.translators.abac_policy import ABACPolicyTranslator


def _ev(kind: LFEventKind, key: str) -> LFEvent:
    return LFEvent(
        kind=kind,
        event_id="evt-1",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=None,
        principal=None,
        permissions=(),
        lf_tags=(LFTagAssignment(key, ""),),
    )


def test_create_lf_tag_emits_upsert_policy():
    tr = ABACPolicyTranslator(namespace_map={"classification": "data_classification"})
    ops = tr.translate(_ev(LFEventKind.CREATE_LF_TAG, "classification"))
    assert len(ops) == 1
    assert ops[0].kind is SyncOpKind.UPSERT_POLICY
    assert ops[0].policy_name == "lf_sync__data_classification"


def test_update_lf_tag_emits_upsert_policy():
    tr = ABACPolicyTranslator(namespace_map={"classification": "data_classification"})
    ops = tr.translate(_ev(LFEventKind.UPDATE_LF_TAG, "classification"))
    assert ops[0].kind is SyncOpKind.UPSERT_POLICY


def test_delete_lf_tag_emits_delete_policy():
    tr = ABACPolicyTranslator(namespace_map={"classification": "data_classification"})
    ops = tr.translate(_ev(LFEventKind.DELETE_LF_TAG, "classification"))
    assert ops[0].kind is SyncOpKind.DELETE_POLICY
    assert ops[0].policy_name == "lf_sync__data_classification"


def test_unknown_key_passthrough_in_policy_name():
    tr = ABACPolicyTranslator(namespace_map={})
    ops = tr.translate(_ev(LFEventKind.CREATE_LF_TAG, "region"))
    assert ops[0].policy_name == "lf_sync__region"


def test_non_tag_lifecycle_event_returns_empty():
    tr = ABACPolicyTranslator(namespace_map={})
    ops = tr.translate(_ev(LFEventKind.GRANT_PERMISSIONS, "classification"))
    assert ops == []
