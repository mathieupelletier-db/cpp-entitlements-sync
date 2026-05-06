"""Tests for TagTranslator: LF-Tag -> UC tag mapping."""
from datetime import datetime, timezone

from entitlements_sync.models import (
    LFEvent,
    LFEventKind,
    LFTagAssignment,
    ResourceRef,
    SyncOpKind,
)
from entitlements_sync.translators.tag import TagTranslator


def _ev(kind: LFEventKind, tags=None) -> LFEvent:
    return LFEvent(
        kind=kind,
        event_id="evt-1",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=None,
        permissions=(),
        lf_tags=tags,
    )


def test_add_known_tag_emits_set_with_managed_marker():
    tr = TagTranslator(namespace_map={"classification": "data_classification"})
    ev = _ev(LFEventKind.ADD_LFTAGS_TO_RESOURCE,
             tags=(LFTagAssignment("classification", "confidential"),))
    ops = tr.translate(ev)
    kinds = [o.kind for o in ops]
    assert SyncOpKind.SET_TAG in kinds
    set_ops = [o for o in ops if o.kind is SyncOpKind.SET_TAG]
    keys_values = {(o.tag_key, o.tag_value) for o in set_ops}
    assert ("data_classification", "confidential") in keys_values
    assert ("managed_by", "lf_sync") in keys_values  # marker tag


def test_add_unknown_tag_passthrough_key():
    tr = TagTranslator(namespace_map={"classification": "data_classification"})
    ev = _ev(LFEventKind.ADD_LFTAGS_TO_RESOURCE,
             tags=(LFTagAssignment("region", "ca-central-1"),))
    ops = tr.translate(ev)
    set_ops = [o for o in ops if o.kind is SyncOpKind.SET_TAG]
    keys_values = {(o.tag_key, o.tag_value) for o in set_ops}
    assert ("region", "ca-central-1") in keys_values


def test_remove_tag_emits_unset():
    tr = TagTranslator(namespace_map={"classification": "data_classification"})
    ev = _ev(LFEventKind.REMOVE_LFTAGS_FROM_RESOURCE,
             tags=(LFTagAssignment("classification", "confidential"),))
    ops = tr.translate(ev)
    unset_ops = [o for o in ops if o.kind is SyncOpKind.UNSET_TAG]
    assert len(unset_ops) == 1
    assert unset_ops[0].tag_key == "data_classification"


def test_non_tag_event_returns_empty():
    tr = TagTranslator(namespace_map={})
    ev = _ev(LFEventKind.GRANT_PERMISSIONS)
    assert tr.translate(ev) == []
