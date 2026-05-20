"""Production reconciler entry point — designed to run as a Databricks Job task.

Reconciler-only design: this is the sole sync mechanism (no event-driven path).
Reads a YAML config, builds the real LF reader, UC client, and audit sink,
constructs the target UC state, runs the reconciler, prints a summary, exits.

Exposed via ``[project.scripts] entitlements-sync = "entitlements_sync.cli:main"``
so the wheel published by ``databricks bundle deploy`` can invoke it as a
``python_wheel_task`` with ``package_name: entitlements_sync, entry_point: main``.

For the legacy laptop workflow (``python scripts/run_sync.py --config ...``),
see ``scripts/run_sync.py`` which is now a thin shim that calls into this
module.

Config (`--config config.yaml`):

    aws:
      region: us-east-1
      catalog_id: "123456789012"
      in_scope:
        - { database: finance, table: trades }
        - { database: finance, table: positions }

    databricks:
      catalog: main              # target UC catalog
      warehouse_id: abc123       # for SQL execution
      audit_table: main.sync_audit.events

    identity:
      group_renames: {}
      iam_role_overrides: {}

    tag_namespace_map:
      classification: data_classification
      lob: business_unit

Usage on Databricks Jobs (via the bundle):

    databricks bundle deploy --target sandbox
    databricks bundle run entitlements_sync_job --target sandbox

Local dry-run (no external calls; synthetic LF state derived from the config):

    python scripts/run_sync.py --config config/config.example.yaml --dry-run

Exit codes:
    0 — reconciled successfully (drift may have been corrected)
    1 — fatal error (unhandled exception)
    2 — config invalid
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Lazy imports inside main() keep --help fast and let unit tests import this
# module without pulling in boto3 / databricks-sdk.

log = logging.getLogger("entitlements_sync.cli")


def load_config(path: Path) -> dict[str, Any]:
    """Load YAML or JSON config. YAML is preferred but optional — falls back to
    JSON so this works in environments without PyYAML."""
    text = path.read_text()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return json.loads(text)


def _validate_config(cfg: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors. Empty list = config is valid."""
    errors: list[str] = []
    for required in ("aws", "databricks", "tag_namespace_map", "identity"):
        if required not in cfg:
            errors.append(f"missing required section: {required}")
            return errors
    for required in ("region", "catalog_id", "in_scope"):
        if required not in cfg["aws"]:
            errors.append(f"missing aws.{required}")
    for required in ("catalog", "warehouse_id", "audit_table"):
        if required not in cfg["databricks"]:
            errors.append(f"missing databricks.{required}")
    return errors


def build_components(cfg: dict[str, Any]):
    """Construct the real components from config. Imports happen here so
    unit tests of ``load_config`` and ``_validate_config`` don't need the deps.

    AWS credential resolution:
      - If ``aws.service_credential_name`` is set in config, the LF + Glue
        clients use short-lived STS credentials obtained from a Databricks
        UC Service Credential via ``WorkspaceClient.credentials.generate_
        temporary_service_credential``. This is the only way to give a
        serverless Databricks Job cross-account AWS access; it also works
        seamlessly from a laptop (no local AWS profile needed).
      - Otherwise, the LF + Glue clients use boto3's default credential
        chain (env vars → ~/.aws/credentials → instance profile → IMDS).
        Use this for local dev with ``aws sso login`` or for classic
        Databricks compute with an instance profile attached.

    The audit sink writes via the same Statement Execution API used for UC
    mutations — no SparkSession required.
    """
    import boto3
    from databricks.sdk import WorkspaceClient

    from .boto_lf_reader import BotoLFReader
    from .databricks_uc import DatabricksUCClient, make_sdk_sql_runner
    from .identity import IdentityResolver
    from .sql_audit import SQLAuditSink

    aws_cfg = cfg["aws"]
    workspace = WorkspaceClient()

    aws_session = _build_aws_session(workspace, aws_cfg)
    lf_client = aws_session.client("lakeformation")
    glue_client = aws_session.client("glue")

    in_scope = _config_in_scope(cfg)
    lf_reader = BotoLFReader(
        client=lf_client,
        catalog_id=aws_cfg["catalog_id"],
        in_scope_resources=in_scope,
        glue_client=glue_client,
    )

    sql_runner = make_sdk_sql_runner(workspace, cfg["databricks"]["warehouse_id"])
    uc = DatabricksUCClient(sql=sql_runner)
    audit = SQLAuditSink(sql=sql_runner, table_name=cfg["databricks"]["audit_table"])

    resolver = IdentityResolver(
        iam_role_overrides=cfg["identity"].get("iam_role_overrides", {}),
        iam_user_overrides=cfg["identity"].get("iam_user_overrides", {}),
        group_renames=cfg["identity"].get("group_renames", {}),
    )

    return lf_reader, uc, audit, resolver


def _build_aws_session(workspace, aws_cfg: dict[str, Any]):
    """Return a ``boto3.Session`` configured per ``aws_cfg``.

    Injects UC Service Credential temp credentials when
    ``aws_cfg["service_credential_name"]`` is set; otherwise constructs a
    session with no explicit credentials and lets boto3's default chain
    resolve them.

    Split out from ``build_components`` so it's mockable in tests and so
    the credential-resolution policy lives in one place.

    The UC Service Credential path goes through ``WorkspaceClient.api_client.do``
    (the low-level REST entry) rather than the SDK's
    ``credentials.generate_temporary_service_credential`` convenience method.
    The convenience method requires a newer ``databricks-sdk`` than the one
    bundled with Databricks serverless compute; the REST endpoint has been
    stable on every SDK release with ``api_client.do``.
    """
    import boto3

    sc_name = aws_cfg.get("service_credential_name")
    region = aws_cfg["region"]

    if not sc_name:
        log.info("AWS auth: boto3 default credential chain (env / ~/.aws / IMDS)")
        return boto3.Session(region_name=region)

    log.info("AWS auth: UC Service Credential %r", sc_name)
    resp = workspace.api_client.do(
        "POST",
        "/api/2.1/unity-catalog/temporary-service-credentials",
        body={"credential_name": sc_name},
    )
    ac = resp["aws_temp_credentials"]
    return boto3.Session(
        aws_access_key_id=ac["access_key_id"],
        aws_secret_access_key=ac["secret_access_key"],
        aws_session_token=ac["session_token"],
        region_name=region,
    )


def build_dry_run_components(cfg: dict[str, Any]):
    """Build in-memory components seeded with synthetic LF state derived from
    the config. No external calls — purely demonstrates the reconciler wiring
    against the real config structure.

    Synthetic LF state:
      - Every in-scope table gets ``classification=internal`` and ``lob=<db>``
        as LF-Tags.
      - Every in-scope table has SELECT granted to the group ``ANALYSTS_CAD``
        and an IAM role grant that lands in the override path.
      - LF-Tag dictionary contains the keys named in ``tag_namespace_map``.
    """
    from .audit import InMemoryAuditSink
    from .identity import IdentityResolver
    from .lf_reader import InMemoryLFReader, LFGrant
    from .models import (
        LFTagAssignment,
        Principal,
        PrincipalKind,
    )
    from .uc_client import InMemoryUCClient

    in_scope = _config_in_scope(cfg)

    tags_by_resource: dict = {}
    grants_by_resource: dict = {}
    for r in in_scope:
        if r.table is not None:
            tags_by_resource[r] = [
                LFTagAssignment("classification", "internal"),
                LFTagAssignment("lob", r.database),
            ]
            grants_by_resource[r] = [
                LFGrant(
                    Principal(PrincipalKind.IDP_GROUP, "ANALYSTS_CAD"),
                    ("SELECT", "DESCRIBE"),
                ),
                LFGrant(
                    Principal(PrincipalKind.IAM_ROLE, "DataPipelineService"),
                    ("SELECT", "INSERT"),
                ),
            ]

    lf_reader = InMemoryLFReader(
        resources=in_scope,
        tags_by_resource=tags_by_resource,
        grants_by_resource=grants_by_resource,
        tag_keys=list(cfg["tag_namespace_map"].keys()),
    )
    uc = InMemoryUCClient()
    audit = InMemoryAuditSink()
    resolver = IdentityResolver(
        iam_role_overrides=cfg["identity"].get("iam_role_overrides", {}),
        iam_user_overrides=cfg["identity"].get("iam_user_overrides", {}),
        group_renames=cfg["identity"].get("group_renames", {}),
    )
    return lf_reader, uc, audit, resolver


def _config_in_scope(cfg: dict[str, Any]):
    from .models import ResourceRef

    catalog = cfg["databricks"]["catalog"]
    return [
        ResourceRef(
            catalog=catalog,
            database=entry["database"],
            table=entry.get("table"),
            column=None,
        )
        for entry in cfg["aws"]["in_scope"]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path, help="Path to YAML or JSON config")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use in-memory fakes seeded from the config; no external calls.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (per-SQL statement, per-LF-read, etc.).",
    )
    args = parser.parse_args(argv)

    # Quieten the noisier transitive loggers unless --verbose is on.
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.verbose:
        for noisy in ("botocore", "urllib3", "databricks.sdk"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    if not args.config.exists():
        log.error("config not found: %s", args.config)
        return 2
    cfg = load_config(args.config)
    errors = _validate_config(cfg)
    if errors:
        for err in errors:
            log.error("config invalid: %s", err)
        return 2

    from .reconciler import Reconciler
    from .target_builder import build_target_state

    if args.dry_run:
        log.info("DRY RUN — in-memory fakes; no external calls")
        lf_reader, uc, audit, resolver = build_dry_run_components(cfg)
    else:
        lf_reader, uc, audit, resolver = build_components(cfg)

    log.info("building target UC state from LF snapshot ...")
    target, build_report = build_target_state(
        reader=lf_reader,
        resolver=resolver,
        tag_namespace_map=cfg["tag_namespace_map"],
    )
    log.info(
        "target built: %d managed resources, %d grant principals, %d policies "
        "(%d identity_unresolved, %d unsupported_perms)",
        len(target.managed_resources),
        sum(len(g) for g in target.grants.values()),
        len(target.policies),
        build_report.identity_unresolved,
        build_report.unsupported_permissions,
    )

    log.info("running reconciler ...")
    rec = Reconciler(uc=uc, audit=audit)
    report = rec.reconcile(target)
    log.info(
        "reconcile complete: %d missing_ops, %d drift_ops, %d audit_rows",
        report.missing_ops,
        report.drift_ops,
        report.audit_rows_written,
    )

    if args.dry_run:
        _print_dry_run_summary(audit, uc, target)

    return 0


def _print_dry_run_summary(audit, uc, target) -> None:
    """Show what the reconciler did against the synthetic state."""
    print()
    print("=" * 72)
    print("DRY-RUN SUMMARY")
    print("=" * 72)
    print(f"Managed resources: {len(target.managed_resources)}")
    print(f"Target policies:   {len(target.policies)}")
    print()
    print(f"Audit rows ({len(audit.rows)}):")
    by_status: dict[str, int] = {}
    by_note: dict[str, int] = {}
    for row in audit.rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_note[row.notes or "<no note>"] = by_note.get(row.notes or "<no note>", 0) + 1
    for status, n in sorted(by_status.items()):
        print(f"  status={status:25s} {n}")
    for note, n in sorted(by_note.items()):
        print(f"  notes={note:25s}  {n}")

    print()
    print("Sample of UC ops applied (first 8):")
    for row in audit.rows[:8]:
        print(
            f"  [{row.notes}] {row.op_kind.value:14s} "
            f"on {row.resource_qualified_name} "
            f"for {row.principal_identifier}"
        )


if __name__ == "__main__":
    sys.exit(main())
