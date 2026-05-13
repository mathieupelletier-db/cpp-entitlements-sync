# Authentication

`scripts/run_sync.py` talks to two clouds:

1. **AWS** — `boto3.client("lakeformation")` and `boto3.client("glue")` read
   tags, grants, and the LF-Tag dictionary from the catalog account
   (`aws.catalog_id` in `config.yaml`, currently `332745928618`,
   `us-west-2`).
2. **Databricks** — `databricks.sdk.WorkspaceClient()` runs SQL via the
   Statement Execution API against the target workspace (the one that owns
   the warehouse in `databricks.warehouse_id`, currently
   `586da169697271c9`) and writes audit rows to
   `databricks.audit_table`.

Both SDKs use their standard credential-resolution chains — there is no
auth code in this repo. As long as `aws sts get-caller-identity` and
`databricks current-user me` work from your shell, `run_sync.py` will
work too.

This doc covers the two runtime contexts:

- [Local development](#local-development) — running `scripts/run_sync.py`
  from a laptop.
- [Databricks Job runtime](#databricks-job-runtime) — the scheduled
  reconciler in production.

---

## Local development

### 1. AWS — SSO login

The Lake Formation account at Databricks Field Engineering is reachable
through the `aws-sandbox-field-eng_databricks-sandbox-admin` SSO profile
(this is the FE-standard sandbox profile; see
`~/.cursor/skills/aws-authentication/SKILL.md` for the wider account
list).

```bash
# One-time: download the FE-wide AWS config if you don't already have it.
# Do NOT clobber an existing ~/.aws/config — append only the missing profile.
curl "https://aws-config.sec.databricks.us/full-config" --output /tmp/aws_config_full
# Inspect /tmp/aws_config_full, copy the relevant [profile ...] block
# into ~/.aws/config if not present, then:
rm -f /tmp/aws_config_full

# Every session:
aws sso login --profile aws-sandbox-field-eng_databricks-sandbox-admin
aws sts get-caller-identity --profile aws-sandbox-field-eng_databricks-sandbox-admin
```

Once SSO is live, point boto3 at the profile by exporting it for the
current shell — `run_sync.py` does not take a `--profile` flag, it picks
up whatever `boto3.client(...)` resolves to:

```bash
export AWS_PROFILE=aws-sandbox-field-eng_databricks-sandbox-admin
export AWS_REGION=us-west-2   # matches aws.region in config.yaml
```

Sanity check that boto3 picked up the same identity:

```bash
python -c "import boto3; print(boto3.client('sts').get_caller_identity())"
```

If `catalog_id` in `config.yaml` is in a different AWS account from your
SSO profile, the profile's role must be able to assume a role in the
catalog account, **or** you must add a profile that does so (standard
boto3 cross-account `role_arn` + `source_profile` config). Boto3 will
auto-assume; no code changes needed here.

### 2. Databricks — CLI login

Pick the profile name that matches the workspace owning
`databricks.warehouse_id` and `databricks.catalog`. For the CPP POC this
is the FE primary demo workspace:

```bash
# One-time per workspace:
databricks auth login \
  https://e2-demo-field-eng.cloud.databricks.com/ \
  --profile e2-demo-west

# Verify:
databricks auth profiles | grep e2-demo-west   # should show YES
databricks current-user me --profile e2-demo-west
```

Then export the profile so the SDK picks it up (the same way boto3 picks
up `AWS_PROFILE`):

```bash
export DATABRICKS_CONFIG_PROFILE=e2-demo-west
```

Sanity check that the SDK resolves the same profile and can reach the
warehouse:

```bash
python -c "
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
print('host:', w.config.host)
print('warehouse:', w.warehouses.get('586da169697271c9').name)
"
```

### 3. Smoke test against the dry-run path

This exercises the wiring without touching either cloud:

```bash
python scripts/run_sync.py --config config/config.yaml --dry-run
```

Then the real thing — reads LF, writes UC, writes audit rows:

```bash
python scripts/run_sync.py --config config/config.yaml
```

---

## Databricks Job runtime

In production, `run_sync.py` runs as a scheduled Databricks Job (default
6h cadence — see `docs/architecture.md`). There is no human in the loop
and no `aws sso login` to run interactively. Auth comes from two sources
attached to the Job's compute.

### Databricks side (`WorkspaceClient()`)

A Job task running on Databricks compute inherits the runtime's
credentials automatically — `WorkspaceClient()` with no arguments
resolves the in-cluster token. No `databricks auth login` required.

Configure the Job to **Run as** a service principal that has:

- `USE CATALOG` on `pension_glue_federated`,
- `USE SCHEMA` + `MODIFY` (or sufficient grants) to issue `GRANT`,
  `REVOKE`, `ALTER TABLE ... SET TAGS`, and the ABAC policy DDL on every
  in-scope resource,
- `CAN USE` on the SQL warehouse `586da169697271c9`,
- write access to `pension_uc_overlay.sync_audit.events`.

Owning the catalog via an account-level group that contains the service
principal is the cleanest way to get all of these in one place.

### AWS side (`boto3.client("lakeformation" / "glue")`)

The Job's cluster needs AWS credentials that can read LF + Glue in the
catalog account (`332745928618`, `us-west-2`). Two patterns work:

1. **Instance profile (classic compute).** Attach an IAM instance
   profile to the cluster. The role it assumes needs read access to LF
   and Glue in the catalog account — minimally:

   ```
   lakeformation:ListPermissions
   lakeformation:GetResourceLFTags
   lakeformation:ListLFTags
   glue:GetTables
   glue:GetDatabases
   ```

   If the Job's account differs from the catalog account, the role must
   be trusted by a role in the catalog account that has those LF/Glue
   permissions, and the trust chain must work via boto3's standard
   assume-role mechanics.

2. **Service Credentials (serverless / preferred).** Register a UC
   Service Credential pointing at an IAM role with the same LF/Glue
   read scope, and resolve it inside the Job before constructing the
   reader. This avoids cluster-level instance profiles and is the path
   we'll likely move to once the POC graduates — for now the Job ships
   with an instance profile because it's simpler to reason about.

Verify in a tiny notebook cell run *inside* the Job's cluster, not on
your laptop:

```python
import boto3
boto3.client("sts").get_caller_identity()
boto3.client("lakeformation", region_name="us-west-2").list_lf_tags(
    CatalogId="332745928618", MaxResults=1
)
```

If both calls succeed, the reconciler will too.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `botocore.exceptions.NoCredentialsError` | `AWS_PROFILE` not exported or SSO session expired | `aws sso login --profile=...` + `export AWS_PROFILE=...` |
| `botocore.exceptions.ClientError: AccessDeniedException` on `list_lf_tags` | Profile is valid but role lacks LF read perms, or wrong `catalog_id` | Confirm `aws sts get-caller-identity` matches an account that has LF read on `aws.catalog_id`; for cross-account, check the assume-role chain |
| `databricks.sdk.errors.DatabricksError: default auth: ...` | No Databricks profile resolvable | `databricks auth login ... --profile=<name>` and `export DATABRICKS_CONFIG_PROFILE=<name>` |
| `Statement execution failed: PERMISSION_DENIED` on `GRANT ...` | Service principal (or your user, locally) lacks ownership on the UC object | Grant `USE CATALOG` / `MODIFY` on the target catalog/schemas, or transfer ownership to an account group that contains the runner |
| Job succeeds locally with `--dry-run` but fails live with `identity_unresolved` audit rows | LF principal isn't in `identity.iam_role_overrides` / `iam_user_overrides` / `group_renames` | Add a mapping in `config.yaml` — see `docs/mappings.md` §3 |
