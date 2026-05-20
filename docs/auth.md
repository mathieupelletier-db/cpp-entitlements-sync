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

The Job's compute needs AWS credentials that can read LF + Glue in the
catalog account (`332745928618`, `us-west-2`). The engine supports two
mechanisms, selected by config (`aws.service_credential_name`):

#### 1. UC Service Credential (default; required for serverless)

Set `aws.service_credential_name: <name>` in `config/config.yaml`. At
runtime, `entitlements_sync.cli._build_aws_session` calls the UC
temporary-credentials REST endpoint
(`POST /api/2.1/unity-catalog/temporary-service-credentials`) and
injects the resulting short-lived STS creds into the boto3 session.
No AWS env vars, no instance profile, no `aws sso login` — Databricks
brokers the AWS access. This is the only mechanism that works on
serverless Databricks compute, and it works identically from a
laptop with no local AWS setup.

One-time setup for a workspace + AWS account pair:

**a. IAM role with LF + Glue read perms.** The reader's required
actions, as called by `BotoLFReader`:

```
lakeformation:ListPermissions
lakeformation:GetResourceLFTags
lakeformation:ListLFTags
glue:GetTables
glue:GetDatabases
glue:GetTable
glue:GetDatabase
```

Plus `sts:AssumeRole` on the role's own ARN (UC requires the role to
be **self-assuming**).

**b. Trust policy.** Two statements: one for the Databricks UC master
role (with the external ID condition), one for self-assume:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Principal": {"AWS": "arn:aws:iam::414351767826:role/unity-catalog-prod-UCMasterRole-14S5ZJVKOTYTL"},
     "Action": "sts:AssumeRole",
     "Condition": {"StringEquals": {"sts:ExternalId": "<your-databricks-account-uuid>"}}},
    {"Effect": "Allow",
     "Principal": {"AWS": "arn:aws:iam::<aws-account>:role/<role-name>"},
     "Action": "sts:AssumeRole"}
  ]
}
```

For the FE one-env Databricks account the external ID is
`0d26daa6-5e44-4c97-a497-ef015f91254a`.

**c. Lake Formation Data Lake Admin grant.** IAM lets the role *call*
the LF APIs; LF itself controls which resources the role can *read*.
For full LF visibility, add the role as a Data Lake Admin:

```bash
# Grab existing settings, append role, put back
aws lakeformation get-data-lake-settings --region us-west-2 > /tmp/lf.json
# Edit /tmp/lf.json to add {"DataLakePrincipalIdentifier": "<role-arn>"}
# to DataLakeSettings.DataLakeAdmins
aws lakeformation put-data-lake-settings --cli-input-json file:///tmp/lf.json --region us-west-2
```

Without this, AWS will return `AccessDeniedException: Insufficient
Lake Formation permission(s) on resource` even though the IAM policy
allows the call.

**d. Register the UC Service Credential.**

```bash
cat > /tmp/sc.json <<'EOF'
{
  "name": "cpp_entitlements_sync_reader",
  "purpose": "SERVICE",
  "aws_iam_role": {
    "role_arn": "arn:aws:iam::<aws-account>:role/<role-name>"
  }
}
EOF
databricks credentials create-credential --json @/tmp/sc.json
```

**e. Reference it in config:**

```yaml
aws:
  service_credential_name: cpp_entitlements_sync_reader
```

#### 2. boto3 default credential chain (laptop dev / classic compute)

Leave `aws.service_credential_name` unset. The engine constructs a
`boto3.Session` without explicit credentials and lets boto3's default
chain resolve them: env vars → `~/.aws/credentials` profile → instance
profile → IMDS. Useful for:

- Local development with `aws sso login --sso-session sandbox` (the
  pattern documented above).
- Classic Databricks compute with an IAM instance profile attached
  (not available in serverless-only workspaces, but the code path
  exists for forward-compat).

#### Verifying either path

Run from your laptop with only `DATABRICKS_CONFIG_PROFILE` set:

```bash
DATABRICKS_CONFIG_PROFILE=<workspace-profile> \
  .venv/bin/python scripts/run_sync.py --config config/config.yaml
```

If `service_credential_name` is set, you should see
`AWS auth: UC Service Credential 'cpp_entitlements_sync_reader'`
in the logs and no need for any AWS env at all. If it's unset, you
should see `AWS auth: boto3 default credential chain` and you'll need
either `aws sso login` or env vars in scope.

For a deployed-Job verification, kick off a run via the bundle and
watch the `run_page_url` it returns:

```bash
databricks bundle run entitlements_sync_job --target sandbox --no-wait
```

The serverless cluster cold-starts in ~60s, then the dry-run reconciler
finishes in <5s.

---

## Deploying the Job (Databricks Asset Bundle)

A Databricks Asset Bundle at the repo root (`databricks.yml`) packages the
wheel, the config, and the Job spec into a single `databricks bundle deploy`
command. One target is defined today (`sandbox`, pointing at
`fe-sandbox-serverless-sandbox-h302gy`); add more under `targets:` as the
project graduates to stable workspaces.

```bash
export DATABRICKS_CONFIG_PROFILE=fe-sandbox-serverless-sandbox-h302gy

databricks bundle validate --target sandbox
databricks bundle deploy   --target sandbox
databricks bundle run entitlements_sync_job --target sandbox
```

What `deploy` does:

1. **Builds the wheel** locally via `uv build --wheel` (the `artifacts:`
   block). Output lands in `dist/entitlements_sync-<version>-py3-none-any.whl`.
2. **Uploads** the wheel + `config/` directory to
   `/Workspace/Users/<you>/.bundle/cpp-entitlements-sync/<target>/`.
3. **Creates / updates the Job** at `https://<workspace>/jobs` with a
   single `reconcile` task running `python_wheel_task` against the wheel.
4. Compute is **serverless** (env `client: "2"`); deps are the wheel
   itself (which pulls in `boto3`, `databricks-sdk`, `pyyaml` transitively).
5. Schedule: `0 0 0/6 * * ?` UTC (00:00, 06:00, 12:00, 18:00),
   **paused** by default so the first deploy never auto-runs.

### Going from dry-run to a real reconcile

The default task ships with `--dry-run` baked into
`python_wheel_task.parameters` so a first deploy is safe even before
the workspace has the catalog / warehouse / AWS access wired up. Two
edits flip the Job to a real run:

1. Remove `"--dry-run"` from `resources.jobs.entitlements_sync_job.tasks[0].python_wheel_task.parameters` in `databricks.yml`.
2. Change `schedule.pause_status` from `PAUSED` to `UNPAUSED`.

Then `databricks bundle deploy --target sandbox` again. The next deploy
is a `MODIFY` operation, not a recreate — the Job ID is preserved, so
any URLs you've shared keep working.

Before flipping, confirm the workspace satisfies the requirements in
the two sections above:

- The UC catalog named in `config/config.yaml: databricks.catalog`
  exists and the running identity has the listed grants.
- The SQL warehouse ID in `config/config.yaml: databricks.warehouse_id`
  exists in the workspace.
- The Job's compute can reach LakeFormation + Glue in
  `config/config.yaml: aws.catalog_id` (Service Credentials on serverless,
  instance profile on classic).

### Iterating after deploy

`databricks bundle deploy` is idempotent — every push updates the Job
in place. To preview what would change without writing:

```bash
databricks bundle deploy --target sandbox --debug    # logs all SDK calls
```

To remove the Job + uploaded files:

```bash
databricks bundle destroy --target sandbox
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `botocore.exceptions.NoCredentialsError` | `AWS_PROFILE` not exported or SSO session expired | `aws sso login --profile=...` + `export AWS_PROFILE=...` |
| `botocore.exceptions.ClientError: AccessDeniedException` on `list_lf_tags` | Profile is valid but role lacks LF read perms, or wrong `catalog_id` | Confirm `aws sts get-caller-identity` matches an account that has LF read on `aws.catalog_id`; for cross-account, check the assume-role chain |
| `databricks.sdk.errors.DatabricksError: default auth: ...` | No Databricks profile resolvable | `databricks auth login ... --profile=<name>` and `export DATABRICKS_CONFIG_PROFILE=<name>` |
| `Statement execution failed: PERMISSION_DENIED` on `GRANT ...` | Service principal (or your user, locally) lacks ownership on the UC object | Grant `USE CATALOG` / `MODIFY` on the target catalog/schemas, or transfer ownership to an account group that contains the runner |
| Job succeeds locally with `--dry-run` but fails live with `identity_unresolved` audit rows | LF principal isn't in `identity.iam_role_overrides` / `iam_user_overrides` / `group_renames` | Add a mapping in `config.yaml` — see `docs/mappings.md` §3 |
