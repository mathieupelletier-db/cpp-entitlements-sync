"""Tests for DatabricksUCClient. Injects a stub SQLRunner; never touches the SDK."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from entitlements_sync.databricks_uc import (
    DatabricksUCClient,
    LoggingABACPolicyAPI,
)
from entitlements_sync.models import (
    Principal,
    PrincipalKind,
    ResourceRef,
    SyncOp,
    SyncOpKind,
)


@dataclass
class RecordingSQLRunner:
    """Stub SQLRunner that records every statement and returns canned rows."""
    statements: list[str] = field(default_factory=list)
    rows_by_pattern: dict[str, list[list[str]]] = field(default_factory=dict)

    def __call__(self, statement: str) -> list[list[str]]:
        self.statements.append(statement)
        for pattern, rows in self.rows_by_pattern.items():
            if pattern in statement:
                return rows
        return []


def _trades() -> ResourceRef:
    return ResourceRef("main", "finance", "trades", None)


def _finance() -> ResourceRef:
    return ResourceRef("main", "finance", None, None)


def _catalog() -> ResourceRef:
    return ResourceRef("main", "", None, None)  # database="" represents catalog-level


# --------------------------- writes ---------------------------------------


def test_set_tag_on_table_issues_alter_table_set_tags():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=_trades(), principal=None, permissions=(),
        tag_key="data_classification", tag_value="confidential", policy_name=None,
    ))
    assert runner.statements == [
        "ALTER TABLE `main`.`finance`.`trades` "
        "SET TAGS ('data_classification' = 'confidential')"
    ]


def test_set_tag_on_schema_issues_alter_schema():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=_finance(), principal=None, permissions=(),
        tag_key="lob", tag_value="AE", policy_name=None,
    ))
    assert runner.statements == [
        "ALTER SCHEMA `main`.`finance` SET TAGS ('lob' = 'AE')"
    ]


def test_unset_tag_issues_unset_tags():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.UNSET_TAG, resource=_trades(), principal=None, permissions=(),
        tag_key="data_classification", tag_value=None, policy_name=None,
    ))
    assert runner.statements == [
        "ALTER TABLE `main`.`finance`.`trades` UNSET TAGS ('data_classification')"
    ]


def test_grant_issues_grant_statement():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=_trades(),
        principal=Principal(PrincipalKind.IDP_GROUP, "ANALYSTS_CAD"),
        permissions=("SELECT", "DESCRIBE"),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    assert runner.statements == [
        "GRANT SELECT, DESCRIBE ON TABLE `main`.`finance`.`trades` TO `ANALYSTS_CAD`"
    ]


def test_revoke_issues_revoke_statement():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.REVOKE, resource=_trades(),
        principal=Principal(PrincipalKind.IDP_GROUP, "ANALYSTS_CAD"),
        permissions=("SELECT",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    assert runner.statements == [
        "REVOKE SELECT ON TABLE `main`.`finance`.`trades` FROM `ANALYSTS_CAD`"
    ]


def test_principal_with_backticks_is_escaped():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.GRANT, resource=_trades(),
        principal=Principal(PrincipalKind.IDP_GROUP, "weird`group"),
        permissions=("SELECT",),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    assert "`weird``group`" in runner.statements[0]


def test_tag_value_with_apostrophe_is_escaped():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.SET_TAG, resource=_trades(), principal=None, permissions=(),
        tag_key="owner", tag_value="O'Brien", policy_name=None,
    ))
    assert "'O''Brien'" in runner.statements[0]


def test_upsert_policy_delegates_to_abac_api():
    runner = RecordingSQLRunner()
    abac = LoggingABACPolicyAPI()
    uc = DatabricksUCClient(sql=runner, abac=abac)
    uc.apply(SyncOp(
        kind=SyncOpKind.UPSERT_POLICY, resource=None, principal=None, permissions=(),
        tag_key=None, tag_value=None, policy_name="lf_sync__data_classification",
    ))
    assert "lf_sync__data_classification" in abac.list()
    assert runner.statements == []  # ABAC does not go through SQL runner


def test_delete_policy_removes_from_abac():
    abac = LoggingABACPolicyAPI()
    abac.upsert("lf_sync__lob")
    uc = DatabricksUCClient(sql=lambda _: [], abac=abac)
    uc.apply(SyncOp(
        kind=SyncOpKind.DELETE_POLICY, resource=None, principal=None, permissions=(),
        tag_key=None, tag_value=None, policy_name="lf_sync__lob",
    ))
    assert abac.list() == set()


def test_none_op_is_silently_skipped():
    runner = RecordingSQLRunner()
    uc = DatabricksUCClient(sql=runner)
    uc.apply(SyncOp(
        kind=SyncOpKind.NONE, resource=None, principal=None, permissions=(),
        tag_key=None, tag_value=None, policy_name=None,
    ))
    assert runner.statements == []


# --------------------------- reads ----------------------------------------


def test_get_tags_on_table_queries_table_tags_view():
    runner = RecordingSQLRunner(rows_by_pattern={
        "table_tags": [
            ["data_classification", "confidential"],
            ["managed_by", "lf_sync"],
        ],
    })
    uc = DatabricksUCClient(sql=runner)
    assert uc.get_tags(_trades()) == {
        "data_classification": "confidential",
        "managed_by": "lf_sync",
    }
    assert "system.information_schema.table_tags" in runner.statements[0]
    assert "table_name = 'trades'" in runner.statements[0]


def test_get_tags_on_schema_queries_schema_tags_view():
    runner = RecordingSQLRunner(rows_by_pattern={"schema_tags": [["lob", "AE"]]})
    uc = DatabricksUCClient(sql=runner)
    assert uc.get_tags(_finance()) == {"lob": "AE"}
    assert "system.information_schema.schema_tags" in runner.statements[0]


def test_get_grants_aggregates_rows():
    runner = RecordingSQLRunner(rows_by_pattern={
        "SHOW GRANTS": [
            ["ANALYSTS_CAD", "SELECT", "TABLE", "main.finance.trades"],
            ["ANALYSTS_CAD", "DESCRIBE", "TABLE", "main.finance.trades"],
            ["RISK_CAD", "SELECT", "TABLE", "main.finance.trades"],
        ],
    })
    uc = DatabricksUCClient(sql=runner)
    assert uc.get_grants(_trades()) == {
        "ANALYSTS_CAD": {"SELECT", "DESCRIBE"},
        "RISK_CAD": {"SELECT"},
    }
    assert "SHOW GRANTS ON TABLE `main`.`finance`.`trades`" in runner.statements[0]


def test_get_policies_returns_abac_state():
    abac = LoggingABACPolicyAPI()
    abac.upsert("lf_sync__lob")
    abac.upsert("lf_sync__data_classification")
    uc = DatabricksUCClient(sql=lambda _: [], abac=abac)
    assert uc.get_policies() == {"lf_sync__lob", "lf_sync__data_classification"}


# --------------------------- guards ---------------------------------------


def test_unknown_op_kind_raises():
    uc = DatabricksUCClient(sql=lambda _: [])
    # Force an unknown kind by patching at call time
    bogus = SyncOp(
        kind=SyncOpKind.SET_TAG, resource=_trades(), principal=None, permissions=(),
        tag_key="k", tag_value="v", policy_name=None,
    )
    object.__setattr__(bogus, "kind", "WAT")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown SyncOpKind"):
        uc.apply(bogus)


def test_column_level_resource_raises_not_implemented():
    uc = DatabricksUCClient(sql=lambda _: [])
    column = ResourceRef("main", "finance", "trades", "ssn")
    with pytest.raises(NotImplementedError):
        uc.apply(SyncOp(
            kind=SyncOpKind.SET_TAG, resource=column, principal=None, permissions=(),
            tag_key="masking", tag_value="hash", policy_name=None,
        ))
