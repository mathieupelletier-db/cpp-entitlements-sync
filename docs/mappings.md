# AWS Glue / Lake Formation → Unity Catalog object mappings

Reference for the translators in this engine. Each translator owns one column of
the mapping below; this doc is the cross-check for what each one is expected to
produce, and for what falls outside its scope.

Conventions:
- LF = AWS Lake Formation. UC = Databricks Unity Catalog.
- "Clean" = isomorphic 1:1. "Lossy" = mappable but with semantic compromise.
  "Gap" = no equivalent today; documented as a roadmap item.
- "POC" / "Out of POC" refers to the scope set in
  `asq/docs/superpowers/specs/2026-05-06-glue-uc-entitlements-sync-design.md`.

---

## 1. Structural objects (Glue Catalog ↔ Unity Catalog)

| AWS / Glue                          | Unity Catalog                              | Status | Notes |
|---|---|---|---|
| Glue Data Catalog (per AWS account/region) | UC Catalog                          | Clean  | Typically 1 Glue catalog → 1 UC catalog. Multi-account Glue → multi-catalog in UC. |
| Glue Database                       | UC Schema                                  | Clean  | Naming usually preserved; some sites prefix with env. |
| Glue Table                          | UC Table (managed or external)             | Lossy  | Iceberg/Delta external = clean path. Hive-only tables may need conversion or stay external. |
| Glue Column                         | UC Column                                  | Clean  | 1:1, but UC enforces stricter type rules. |
| Glue Partition                      | (none as a UC object)                      | n/a    | UC tables have partitions but they aren't grantable objects. |
| Glue Function                       | UC SQL/Python Function                     | Gap    | Different definition language; not part of entitlements sync. |
| S3 path registered in LF            | UC **External Location** + **Storage Credential** | Lossy  | LF's "register location with role X" splits into two UC objects. |

## 2. Principals (LF DataLakePrincipal ↔ UC identity)

Parsing of the LF `DataLakePrincipalIdentifier` string is in
`entitlements_sync.boto_lf_reader.default_principal_parser`; the resulting
`Principal` is then mapped to a UC identity by
`entitlements_sync.identity.IdentityResolver`.

| LF principal                              | UC equivalent                  | Status | Resolution rule |
|---|---|---|---|
| IAM Identity Center **group** (preferred) | UC account-level group         | Clean  | Match by display name (SCIM-fed from same IdP). Happy path. |
| IAM Identity Center **user**              | UC account-level user          | Clean  | Match by email / UPN. |
| Federated SAML user                       | UC user                        | Clean  | Email / UPN match — see "ARN shapes" below. |
| IAM **role**                              | (no direct equivalent)         | Gap    | Needs explicit row in `identity.iam_role_overrides`. Common edge case. |
| IAM **user** (non-federated)              | (no direct equivalent)         | Gap    | Same — `identity.iam_user_overrides` or skip. |
| LF "data lake admin"                      | UC metastore admin + catalog owner | Lossy | Coarse mapping; usually manual, not auto-synced. |

### LF principal ARN shapes the parser recognizes

LF stores grants under several distinct ARN shapes depending on how the grant
was issued and which AWS service captured it. The parser checks the federated
shapes (rows 1–2 below) **before** the plain IAM role/user shapes (rows 3–4),
because the federated shapes also contain `:role/` or `:user/` segments and
would otherwise be misclassified.

| LF ARN shape | Example | Parsed as | Lookup key |
|---|---|---|---|
| `arn:aws:iam::<acct>:saml-provider/<provider>:user/<nameid>` | `arn:aws:iam::332745928618:saml-provider/AWSReservedSSO_databricks-sandbox-admin_<hash>:user/ron.guerrero@databricks.com` | `IDP_USER(<nameid>)` if NameID contains `@`; otherwise `IAM_USER(<full ARN>)` | UC SCIM email match (no override); full ARN in `iam_user_overrides` for non-email NameIDs |
| `arn:aws:sts::<acct>:assumed-role/<role>/<session>` | `arn:aws:sts::332745928618:assumed-role/AWSReservedSSO_databricks-sandbox-admin_<hash>/ron.guerrero@databricks.com` | `IDP_USER(<session>)` if session name contains `@`; otherwise `IAM_ROLE(<role>)` | UC SCIM email match (no override); role name in `iam_role_overrides` for service sessions |
| `arn:aws:iam::<acct>:role/<role>` | `arn:aws:iam::123:role/AnalystRole` | `IAM_ROLE(<role>)` | `<role>` in `iam_role_overrides` |
| `arn:aws:iam::<acct>:user/<path/name>` | `arn:aws:iam::123:user/demo/pension-ron` | `IAM_USER(<path/name>)` | `<path/name>` in `iam_user_overrides` |
| `arn:aws:identitystore::<acct>:group/<id>/<group-id>` or `arn:aws:sso:…` | `arn:aws:identitystore::123:group/d-x/12345…` | `IDP_GROUP(<group-id>)` | `group_renames` to map to UC display name |
| Bare email (no ARN) | `alice@cpp.example` | `IDP_USER(alice@cpp.example)` | Direct UC SCIM match |
| Bare name (no ARN, no `@`) | `ANALYSTS_CAD` | `IDP_GROUP(ANALYSTS_CAD)` | `group_renames` if display names diverge |

**The two SSO shapes (rows 1–2) are the ones to watch.** AWS Identity Center
emits the SAML-provider:user form in some LF code paths and the
sts:assumed-role form in others, for the *same human*. Both auto-resolve to
`IDP_USER(email)` so the operator doesn't have to maintain duplicate override
rows for the same person.

## 3. Permissions / privileges

Implemented by `entitlements_sync.privilege_mapping.map_lf_to_uc_privileges`.
Both `translators.grant.GrantTranslator` (event path) and
`target_builder.build_target_state` (reconciler path) go through it.

This is the messiest mapping because LF and UC don't have isomorphic verb
sets **and the right UC verb depends on the resource level**. Two real-world
discoveries from the CPP run prompted the level-aware table:

1. UC has no table-level `DESCRIBE`. SELECT implies metadata access.
   `GRANT DESCRIBE ON TABLE` simply fails.
2. UC folds `INSERT` / `DELETE` / `ALTER` (on table) into a single `MODIFY`
   privilege.

| LF permission           | At CATALOG       | At SCHEMA          | At TABLE          | Status |
|---|---|---|---|---|
| `SELECT`                | n/a              | n/a                | `SELECT`          | Clean (table only) |
| `DESCRIBE`              | `USE CATALOG`    | `USE SCHEMA`       | **drop** (SELECT implies metadata) | Lossy; level-aware |
| `INSERT`                | n/a              | n/a                | `MODIFY`          | Lossy (folded) |
| `DELETE`                | n/a              | n/a                | `MODIFY`          | Lossy (folded) |
| `ALTER`                 | n/a              | n/a                | `MODIFY`          | Lossy (folded) |
| `DROP`                  | **drop**         | **drop**           | **drop**          | Gap (UC ownership only — not grantable) |
| `ALL`                   | `ALL PRIVILEGES` | `ALL PRIVILEGES`   | `ALL PRIVILEGES`  | Clean |
| `CREATE_TABLE`          | n/a              | `CREATE TABLE`     | n/a               | Clean |
| `CREATE_DATABASE`       | `CREATE SCHEMA`  | n/a                | n/a               | Clean |
| `DATA_LOCATION_ACCESS`  | (handled on UC External Location: `READ FILES` / `WRITE FILES` / `CREATE EXTERNAL TABLE`) | | | Lossy |
| `SUPER`                 | (no equivalent — drop) | (drop)       | (drop)            | Gap |
| `WITH GRANT OPTION`     | (no per-privilege equivalent — drop)             | | | Gap (use UC ownership / `MANAGE`) |
| `ASSOCIATE` (LF-Tag → resource) | (handled by Tag + ABAC translators, not GrantTranslator) | | | n/a |

"n/a" = LF doesn't define this permission at this level (LF rejects the grant
upstream). "drop" = mapped to None in the table; the LF permission is reported
in the audit's `unsupported_permissions` counter and the grant is skipped.

## 4. Tag-based access control (the dominant CPP pattern)

Handled by `entitlements_sync.translators.tag.TagTranslator` (object tags) and
`entitlements_sync.translators.abac_policy.ABACPolicyTranslator` (policies).

| LF                                  | UC                                            | Status | How the engine handles it |
|---|---|---|---|
| LF-Tag (key, allowed values)        | UC tag (key/value)                            | Clean  | TagTranslator mirrors with optional namespace remap (e.g., `classification` → `data_classification`). |
| LF-Tag association on a resource    | UC tag on schema/table/column                 | Clean  | Same translator. Idempotent. |
| LF-Tag policy (grant on a tag expression) | UC ABAC policy referencing the mirrored tag | Lossy  | ABACPolicyTranslator. Equality and conjunction expressions are clean; some operators need lowering. |
| LF-Tag policy operator (`equals`, `in`, boolean across keys) | UC ABAC policy expression | Lossy | UC supports tag-equality and conjunctions; rare unsupported ops get logged as `gap_kind=abac_expressivity`. |
| Untag operation                     | UC tag removal + ABAC re-evaluation           | Clean  | Cascade: tag removed → ABAC still applies via inheritance rules. |

## 5. Row/cell-level filters and column masks

**Out of POC.** Documented as honest gaps to the MD.

| LF                                       | UC                                                | Status | Mapping status |
|---|---|---|---|
| LF row filter (predicate on rows)        | UC **row filter** (function attached via `SET ROW FILTER`) | Lossy  | Mappable in principle; predicate languages differ. Out of POC. |
| LF column exclusion in a data filter     | UC column-level SELECT grant **or** column mask   | Lossy  | Two distinct UC mechanisms depending on intent. |
| LF cell filter with masking function     | UC **column mask** (function attached via `SET MASK`) | Lossy  | Mappable; needs function port. Out of POC. |
| LF "all rows / specific columns"         | UC SELECT on enumerated columns                   | Clean  | Cleanest case. |

## 6. Sharing / federation (out of CPP scope — for orientation)

| AWS                                 | UC equivalent                            | Status | Notes |
|---|---|---|---|
| LF cross-account share (RAM)        | UC **Delta Sharing**                     | Gap    | Different model entirely. Not synced. |
| LF resource link                    | UC view or foreign catalog               | Gap    | No direct sync; manual choice. |
| Glue federated catalog (UC reads Glue) | UC **foreign catalog** on Glue        | n/a    | Read-only metadata federation — separate path from entitlements sync. Could combine (federation for reads + sync for write-target tables) but it's a different problem. |

---

## How this maps onto the engine

| Engine component                                           | Sections owned |
|---|---|
| `translators/tag.py` → `TagTranslator`                     | §4 rows 1–2 (object-level tag mirror) |
| `translators/abac_policy.py` → `ABACPolicyTranslator`      | §4 rows 3–5 (tag-policy translation) |
| `translators/grant.py` → `GrantTranslator`                 | §3 (verb-level grants on objects) + §1 last row (External Location grants from `DATA_LOCATION_ACCESS`) |
| `identity.py` → `IdentityResolver`                         | §2 (principal mapping) |
| `reconciler.py` → `Reconciler`                             | Full-state diff across §1 + §3 + §4; safety net when events are missed |
| `orchestrator.py` → `SyncOrchestrator`                     | Event classification, translator dispatch, audit write |
| `cloudtrail.py`                                            | Parses §3/§4 mutations from `lakeformation.amazonaws.com` events |

Deliberately deferred (POC honest gaps):
- §5 entirely (row/cell filters, masks)
- §6 entirely (sharing, federation)
- §2 rows 4–5 are partial — override table only

---

## Worked example: an LF-Tag policy change

To make the dispatch concrete, here is what a single LF mutation produces:

**LF event:** `GrantPermissions` for `SELECT` on tag expression
`classification IN (public, internal)` to IdP group `ANALYSTS_CAD`.

**Engine dispatch:**
1. `cloudtrail.py` parses the CloudTrail record into an `LFEvent` dataclass.
2. `orchestrator.py` classifies it as a tag-policy change (not a resource grant).
3. `IdentityResolver` maps `ANALYSTS_CAD` → UC group `ANALYSTS_CAD` by display
   name (§2 row 1, clean).
4. `TagTranslator` ensures the `classification` tag exists in UC and is allowed
   to take values `public`, `internal`, `confidential` (§4 row 1).
5. `ABACPolicyTranslator` upserts a UC ABAC policy: "group `ANALYSTS_CAD`
   has `SELECT` on tables where tag `classification IN (public, internal)`"
   (§4 row 3).
6. `audit.py` writes one row per UC op with `source=event`,
   `lf_event_id=<CloudTrail eventID>`, `status=ok`.
7. Next `Reconciler` pass sees no drift; no corrective ops.

The same event in the **reconciler path** (e.g., if the Lambda missed it):
2'. `reconciler.py` enumerates LF state, finds the policy not yet in UC, and
    re-issues the same translator calls with `source=reconciler` in audit.
