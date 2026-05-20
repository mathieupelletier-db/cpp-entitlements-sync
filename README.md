# entitlements-sync

LF → UC entitlements sync engine. CPP POC (Plan 1: engine core).

## Docs

- [`docs/project-overview.html`](docs/project-overview.html) — six-slide high-level deck (Amplify style, dark theme, responsive). Cover, problem, approach, architecture, what gets synced, phasing.
- [`docs/project-overview-keynote.html`](docs/project-overview-keynote.html) — same six-slide content, Databricks keynote style (1920×1080 fixed canvas, light theme, DM Sans + IBM Plex Sans, native PDF export via Chrome Print).
- [`docs/project-overview-obsidian.md`](docs/project-overview-obsidian.md) — same six-slide content, reveal.js via the Obsidian Slides Extended plugin (1280×720 canvas, DM Sans, fragments, mermaid). Requires Obsidian with the Slides Extended community plugin enabled; uses `docs/slides-css/databricks.css`.
- [`docs/architecture.md`](docs/architecture.md) — high-level architecture diagram, two paths (event-driven + reconciler), component-to-file map.
- [`docs/mappings.md`](docs/mappings.md) — AWS Glue / Lake Formation → Unity Catalog object mapping reference (structural, principals, privileges, tags, filters, sharing).
- [`docs/auth.md`](docs/auth.md) — how to log in to AWS (SSO) and Databricks (CLI) for local runs, plus the service-principal + IAM role setup the scheduled Job uses.
- Design spec lives in the parent `asq` repo:
  `docs/superpowers/specs/2026-05-06-glue-uc-entitlements-sync-design.md`.

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

Before running `scripts/run_sync.py` against real clouds, log in to both
AWS and Databricks — see [`docs/auth.md`](docs/auth.md).

## Deploying as a Databricks Job

A Databricks Asset Bundle (`databricks.yml`) at the repo root packages the wheel, the config, and the Job spec. After a one-time `databricks auth login`:

```bash
export DATABRICKS_CONFIG_PROFILE=<your-workspace-profile>
databricks bundle validate --target sandbox
databricks bundle deploy   --target sandbox
databricks bundle run entitlements_sync_job --target sandbox
```

The default ships with `--dry-run` and a paused schedule so the first deploy is always safe. See [`docs/auth.md`](docs/auth.md) for the full story (target customization, going from dry-run to a real reconcile, required workspace state).

## Scripts

- `scripts/run_sync.py` — thin shim around `entitlements_sync.cli.main` for the legacy `python scripts/run_sync.py --config ...` workflow. The Databricks Job uses the wheel directly via the bundle's `python_wheel_task`. See [`config/config.example.yaml`](config/config.example.yaml) for a worked config.
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
| `audit.py` / `sql_audit.py`            | Audit sink Protocol + in-memory fake / SQL-INSERT-backed real impl |
| `identity.py`                          | Map LF principals to UC identities                      |
| `translators/{tag,abac_policy,grant}.py` | Per-event translators (used by `target_builder` + the optional event path) |
| `cloudtrail.py` / `orchestrator.py`    | **Unused.** Event-driven sync path; kept for future sub-minute SLA needs |
