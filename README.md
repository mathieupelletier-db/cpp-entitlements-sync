# entitlements-sync

LF → UC entitlements sync engine. CPP POC (Plan 1: engine core).

## Docs

- [`docs/architecture.md`](docs/architecture.md) — high-level architecture diagram, two paths (event-driven + reconciler), component-to-file map.
- [`docs/mappings.md`](docs/mappings.md) — AWS Glue / Lake Formation → Unity Catalog object mapping reference (structural, principals, privileges, tags, filters, sharing).
- Design spec lives in the parent `asq` repo:
  `docs/superpowers/specs/2026-05-06-glue-uc-entitlements-sync-design.md`.

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## Scripts

- `scripts/run_sync.py` — **production entry point** for the reconciler Job. Reads YAML/JSON config, builds the real `BotoLFReader` / `DatabricksUCClient` / `DeltaAuditSink`, runs end-to-end. Run as a Databricks Job task. See [`config/config.example.yaml`](config/config.example.yaml) for a worked example.
- `scripts/run_reconciler.py` — local demo with fixture data; demonstrates drift detection + correction against the in-memory client.
- `scripts/run_local.py` — local demo of the event-path (unused in the reconciler-only design; kept for reference).

## Module layout

| Module                                 | Purpose                                                 |
|---|---|
| `lf_reader.py`                         | `LFReader` Protocol + `InMemoryLFReader` test fake      |
| `boto_lf_reader.py`                    | Real LF reader (boto3 LakeFormation client)             |
| `target_builder.py`                    | Build `TargetUCState` from an LF snapshot               |
| `reconciler.py`                        | Diff target vs current UC, emit corrective ops          |
| `uc_client.py`                         | `UCClient` Protocol + `InMemoryUCClient` test fake      |
| `databricks_uc.py`                     | Real UC client (Statement Execution API + ABAC adapter) |
| `audit.py` / `delta_audit.py`          | Audit sink Protocol + in-memory fake / Delta-backed real impl |
| `identity.py`                          | Map LF principals to UC identities                      |
| `translators/{tag,abac_policy,grant}.py` | Per-event translators (used by `target_builder` + the optional event path) |
| `cloudtrail.py` / `orchestrator.py`    | **Unused.** Event-driven sync path; kept for future sub-minute SLA needs |
