"""Smoke tests for the InMemoryLFReader fake — the Protocol shape itself isn't testable directly."""
from entitlements_sync.lf_reader import InMemoryLFReader, LFGrant
from entitlements_sync.models import LFTagAssignment, Principal, PrincipalKind, ResourceRef


def _trades() -> ResourceRef:
    return ResourceRef("123456789012", "finance", "trades", None)


def test_empty_reader_returns_empty_lists():
    r = InMemoryLFReader()
    assert r.list_in_scope_resources() == []
    assert r.list_lf_tag_keys() == []
    assert r.get_lf_tags_on_resource(_trades()) == []
    assert r.list_grants_on_resource(_trades()) == []


def test_populated_reader_returns_copies():
    trades = _trades()
    grant = LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("SELECT",))
    tag = LFTagAssignment("classification", "confidential")
    r = InMemoryLFReader(
        resources=[trades],
        tags_by_resource={trades: [tag]},
        grants_by_resource={trades: [grant]},
        tag_keys=["classification"],
    )
    assert r.list_in_scope_resources() == [trades]
    assert r.get_lf_tags_on_resource(trades) == [tag]
    assert r.list_grants_on_resource(trades) == [grant]
    assert r.list_lf_tag_keys() == ["classification"]


def test_results_are_independent_copies():
    """Mutating the returned list must not corrupt the reader's state."""
    trades = _trades()
    tag = LFTagAssignment("classification", "confidential")
    r = InMemoryLFReader(tags_by_resource={trades: [tag]})
    out = r.get_lf_tags_on_resource(trades)
    out.append(LFTagAssignment("rogue", "value"))
    assert r.get_lf_tags_on_resource(trades) == [tag]
