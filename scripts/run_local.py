#!/usr/bin/env python
"""Run the sync engine end-to-end against a CloudTrail fixture file.

Usage:
    python scripts/run_local.py tests/fixtures/lf_events/grant_permissions.json
    python scripts/run_local.py tests/fixtures/lf_events/*.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from pprint import pprint

from entitlements_sync.audit import InMemoryAuditSink
from entitlements_sync.cloudtrail import parse_event
from entitlements_sync.identity import IdentityResolver
from entitlements_sync.orchestrator import SyncOrchestrator
from entitlements_sync.translators.abac_policy import ABACPolicyTranslator
from entitlements_sync.translators.grant import GrantTranslator
from entitlements_sync.translators.tag import TagTranslator
from entitlements_sync.uc_client import InMemoryUCClient

NAMESPACE = {"classification": "data_classification", "lob": "line_of_business"}


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    identity_file = repo_root / "tests" / "fixtures" / "identity_mapping.json"

    uc = InMemoryUCClient()
    audit = InMemoryAuditSink()
    orch = SyncOrchestrator(
        uc=uc,
        audit=audit,
        identity=IdentityResolver.from_file(identity_file),
        tag_translator=TagTranslator(namespace_map=NAMESPACE),
        abac_translator=ABACPolicyTranslator(namespace_map=NAMESPACE),
        grant_translator=GrantTranslator(),
    )

    for path in paths:
        rec = json.loads(Path(path).read_text())
        ev = parse_event(rec)
        print(f"\n--- handling {path} ({ev.kind.value}) ---")
        orch.handle(ev)

    print("\n=== UC tags ===")
    pprint(uc._tags)
    print("\n=== UC grants ===")
    pprint(uc._grants)
    print("\n=== UC policies ===")
    pprint(uc._policies)
    print("\n=== Audit rows ===")
    for row in audit.rows:
        print(f"  [{row.status}] {row.op_kind.value} on {row.resource_qualified_name} "
              f"for {row.principal_identifier} (event {row.source_event_id})"
              + (f" — note: {row.notes}" if row.notes else "")
              + (f" — error: {row.error}" if row.error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
