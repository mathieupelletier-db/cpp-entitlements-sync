#!/usr/bin/env python
"""Demonstrate the reconciler against a fixture target state.

Builds a UC state with deliberate drift (a stale tag and an orphan grant) and a target
state describing what UC SHOULD look like. Runs the reconciler and prints corrections.
"""
from __future__ import annotations

import sys
from pprint import pprint

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.models import Principal, PrincipalKind, ResourceRef, SyncOp, SyncOpKind
from entitlements_sync.reconciler import Reconciler, TargetUCState
from entitlements_sync.uc_client import InMemoryUCClient


def main() -> int:
    uc = InMemoryUCClient()
    audit = InMemoryAuditSink()

    r = ResourceRef("123456789012", "finance", "trades", None)

    # Pre-populate UC with drift: a stale tag value AND an orphan grant
    uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="data_classification", tag_value="public",  # WRONG: target says confidential
        policy_name=None,
    ))
    uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=r, principal=None, permissions=(),
        tag_key="managed_by", tag_value="lf_sync", policy_name=None,
    ))
    uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=r,
        principal=Principal(PrincipalKind.IDP_GROUP, "ex-employees"),  # WRONG: not in target
        permissions=("SELECT",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    uc.apply(SyncOp(
        kind=SyncOpKind.UPSERT_POLICY, resource=None, principal=None, permissions=(),
        tag_key=None, tag_value=None, policy_name="orphan_policy",  # WRONG: not in target
    ))

    print("=== UC state BEFORE reconcile ===")
    print("tags:", uc.get_tags(r))
    print("grants:", uc.get_grants(r))
    print("policies:", uc.get_policies())

    target = TargetUCState(
        tags={r: {"data_classification": "confidential"}},
        grants={r: {"data-analysts": {"SELECT"}}},
        policies={"lf_sync__data_classification"},
        managed_resources={r},
    )

    rec = Reconciler(uc=uc, audit=audit)
    report = rec.reconcile(target)

    print("\n=== UC state AFTER reconcile ===")
    print("tags:", uc.get_tags(r))
    print("grants:", uc.get_grants(r))
    print("policies:", uc.get_policies())

    print("\n=== Reconcile report ===")
    pprint(report)

    print("\n=== Audit rows ===")
    for row in audit.rows:
        print(f"  [{row.notes}] {row.op_kind.value} on {row.resource_qualified_name} "
              f"for {row.principal_identifier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
