"""Tests for GrantTranslator: LF resource grants -> UC GRANT/REVOKE ops."""
from datetime import datetime, timezone

from entitlements_sync.models import (
    LFEvent,
    LFEventKind,
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOpKind,
)
from entitlements_sync.translators.grant import GrantTranslator, UnsupportedPermissions


def _ev(kind: LFEventKind, perms: tuple[str, ...]) -> LFEvent:
    return LFEvent(
        kind=kind,
        event_id="evt-1",
        event_time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        resource=ResourceRef("prod", "finance", "trades", None),
        principal=Principal(PrincipalKind.IDP_GROUP, "data-analysts"),
        permissions=perms,
        lf_tags=None,
    )


def test_grant_select_translates_to_grant_op():
    tr = GrantTranslator()
    ops, unsupported = tr.translate(_ev(LFEventKind.GRANT_PERMISSIONS, ("SELECT",)))
    assert len(ops) == 1
    assert ops[0].kind is SyncOpKind.GRANT
    assert ops[0].permissions == ("SELECT",)
    assert unsupported == UnsupportedPermissions(perms=())


def test_grant_multiple_permissions_grouped_into_one_op():
    tr = GrantTranslator()
    ops, _ = tr.translate(_ev(LFEventKind.GRANT_PERMISSIONS, ("SELECT", "DESCRIBE")))
    assert len(ops) == 1
    assert set(ops[0].permissions) == {"SELECT", "DESCRIBE"}


def test_revoke_translates_to_revoke_op():
    tr = GrantTranslator()
    ops, _ = tr.translate(_ev(LFEventKind.REVOKE_PERMISSIONS, ("SELECT",)))
    assert ops[0].kind is SyncOpKind.REVOKE


def test_unsupported_permission_dropped_and_reported():
    tr = GrantTranslator()
    ops, unsupported = tr.translate(_ev(LFEventKind.GRANT_PERMISSIONS, ("SELECT", "ASSOCIATE")))
    assert ops[0].permissions == ("SELECT",)
    assert unsupported.perms == ("ASSOCIATE",)


def test_only_unsupported_returns_no_ops():
    tr = GrantTranslator()
    ops, unsupported = tr.translate(_ev(LFEventKind.GRANT_PERMISSIONS, ("ASSOCIATE",)))
    assert ops == []
    assert unsupported.perms == ("ASSOCIATE",)


def test_non_grant_event_returns_empty():
    tr = GrantTranslator()
    ops, _ = tr.translate(_ev(LFEventKind.ADD_LFTAGS_TO_RESOURCE, ()))
    assert ops == []
