"""Tests for build_target_state: LF state -> TargetUCState."""
import pytest

from entitlements_sync.identity import IdentityResolver
from entitlements_sync.lf_reader import InMemoryLFReader, LFGrant
from entitlements_sync.models import LFTagAssignment, Principal, PrincipalKind, ResourceRef
from entitlements_sync.target_builder import build_target_state, BuildReport
from entitlements_sync.translators.abac_policy import POLICY_NAME_PREFIX


def _trades() -> ResourceRef:
    return ResourceRef("123456789012", "finance", "trades", None)


def _resolver() -> IdentityResolver:
    return IdentityResolver(iam_role_overrides={}, group_renames={})


def test_empty_reader_produces_empty_target():
    target, report = build_target_state(
        reader=InMemoryLFReader(),
        resolver=_resolver(),
        tag_namespace_map={},
    )
    assert target.tags == {}
    assert target.grants == {}
    assert target.policies == set()
    assert target.managed_resources == set()
    assert report.identity_unresolved == 0
    assert report.unsupported_permissions == 0


def test_tags_are_mapped_via_namespace_map_and_become_managed_resources():
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        tags_by_resource={trades: [LFTagAssignment("classification", "confidential")]},
    )
    target, _ = build_target_state(
        reader=reader,
        resolver=_resolver(),
        tag_namespace_map={"classification": "data_classification"},
    )
    assert target.tags[trades] == {"data_classification": "confidential"}
    assert trades in target.managed_resources


def test_unmapped_tag_keys_pass_through():
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        tags_by_resource={trades: [LFTagAssignment("lob", "AE")]},
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={"classification": "data_classification"},
    )
    assert target.tags[trades] == {"lob": "AE"}


def test_grants_resolve_principals_and_translate_to_uc_privileges():
    """SELECT passes through on a TABLE. DESCRIBE on TABLE drops (UC has no
    table-level describe; SELECT implies it). SUPER is unknown.
    """
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        grants_by_resource={trades: [
            LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"),
                    ("SELECT", "DESCRIBE", "SUPER")),
        ]},
    )
    target, report = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    assert target.grants[trades] == {"analysts": {"SELECT"}}
    assert report.unsupported_permissions == 2  # DESCRIBE-at-table + SUPER


def test_unresolved_iam_role_grant_is_skipped_and_counted():
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        grants_by_resource={trades: [
            LFGrant(Principal(PrincipalKind.IAM_ROLE, "arn:aws:iam::123:role/Ghost"),
                    ("SELECT",)),
        ]},
    )
    target, report = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    assert trades not in target.grants  # no row created at all
    assert report.identity_unresolved == 1


def test_one_policy_materialized_per_lf_tag_key_with_namespace_map():
    reader = InMemoryLFReader(tag_keys=["classification", "lob"])
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(),
        tag_namespace_map={"classification": "data_classification"},
    )
    assert target.policies == {
        f"{POLICY_NAME_PREFIX}data_classification",
        f"{POLICY_NAME_PREFIX}lob",
    }


def test_resource_with_grants_only_is_still_managed():
    """A resource with grants but no LF-Tags is still under management."""
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        grants_by_resource={trades: [
            LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("SELECT",)),
        ]},
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    assert trades in target.managed_resources


def test_table_grant_synthesizes_parent_use_chain():
    """A SELECT on a TABLE must also produce additive USE SCHEMA on the parent
    schema and USE CATALOG on the parent catalog. UC requires the full chain."""
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        grants_by_resource={trades: [
            LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("SELECT",)),
        ]},
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    catalog_ref = ResourceRef("123456789012", "", None, None)
    schema_ref = ResourceRef("123456789012", "finance", None, None)
    assert target.additive_grants[catalog_ref] == {"analysts": {"USE CATALOG"}}
    assert target.additive_grants[schema_ref] == {"analysts": {"USE SCHEMA"}}


def test_schema_grant_synthesizes_only_use_catalog():
    """A grant on a SCHEMA needs USE CATALOG on the parent — but not USE SCHEMA
    on itself (the LF DESCRIBE-on-database already mapped to USE SCHEMA in the
    main grants)."""
    schema = ResourceRef("123456789012", "finance", None, None)
    reader = InMemoryLFReader(
        resources=[schema],
        grants_by_resource={schema: [
            LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("DESCRIBE",)),
        ]},
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    catalog_ref = ResourceRef("123456789012", "", None, None)
    assert target.additive_grants[catalog_ref] == {"analysts": {"USE CATALOG"}}
    # No additive grant on the schema itself — the main grants already cover it.
    assert schema not in target.additive_grants


def test_multiple_principals_aggregate_in_additive():
    trades = _trades()
    positions = ResourceRef("123456789012", "finance", "positions", None)
    reader = InMemoryLFReader(
        resources=[trades, positions],
        grants_by_resource={
            trades: [LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("SELECT",))],
            positions: [LFGrant(Principal(PrincipalKind.IDP_GROUP, "risk"), ("SELECT",))],
        },
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    catalog_ref = ResourceRef("123456789012", "", None, None)
    # Both principals need USE CATALOG on the same catalog
    assert target.additive_grants[catalog_ref] == {
        "analysts": {"USE CATALOG"},
        "risk": {"USE CATALOG"},
    }


def test_no_grants_means_no_additive_grants():
    """A resource with only tags (no grants) shouldn't synthesize USE grants."""
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        tags_by_resource={trades: [LFTagAssignment("classification", "confidential")]},
    )
    target, _ = build_target_state(
        reader=reader, resolver=_resolver(), tag_namespace_map={},
    )
    assert target.additive_grants == {}


def test_identity_resolver_rename_is_applied_to_grant_principals():
    trades = _trades()
    reader = InMemoryLFReader(
        resources=[trades],
        grants_by_resource={trades: [
            LFGrant(Principal(PrincipalKind.IDP_GROUP, "analysts"), ("SELECT",)),
        ]},
    )
    resolver = IdentityResolver(
        iam_role_overrides={},
        group_renames={"analysts": "data-analysts"},
    )
    target, _ = build_target_state(
        reader=reader, resolver=resolver, tag_namespace_map={},
    )
    assert target.grants[trades] == {"data-analysts": {"SELECT"}}
