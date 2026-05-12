"""Tests for BotoLFReader. Uses botocore.stub.Stubber so we never call AWS."""
import boto3
import pytest
from botocore.stub import Stubber

from entitlements_sync.boto_lf_reader import (
    BotoLFReader,
    default_principal_parser,
)
from entitlements_sync.models import (
    LFTagAssignment,
    Principal,
    PrincipalKind,
    ResourceRef,
)

CATALOG_ID = "123456789012"


def _trades() -> ResourceRef:
    return ResourceRef(CATALOG_ID, "finance", "trades", None)


@pytest.fixture
def client_and_stub():
    client = boto3.client("lakeformation", region_name="us-east-1")
    stubber = Stubber(client)
    yield client, stubber
    stubber.deactivate()


# --- default principal parser ----------------------------------------------


def test_parser_recognises_iam_role_arn():
    p = default_principal_parser("arn:aws:iam::123456789012:role/AnalystRole")
    assert p == Principal(PrincipalKind.IAM_ROLE, "AnalystRole")


def test_parser_recognises_iam_user_arn():
    p = default_principal_parser("arn:aws:iam::332745928618:user/demo/pension-ron")
    assert p == Principal(PrincipalKind.IAM_USER, "demo/pension-ron")


def test_parser_recognises_iam_user_arn_without_path():
    p = default_principal_parser("arn:aws:iam::123:user/jane.doe")
    assert p == Principal(PrincipalKind.IAM_USER, "jane.doe")


def test_parser_recognises_idc_group_arn():
    p = default_principal_parser(
        "arn:aws:identitystore::123:group/d-1234abcd/12345678-1234-1234-1234-123456789012"
    )
    assert p.kind is PrincipalKind.IDP_GROUP
    assert p.identifier == "12345678-1234-1234-1234-123456789012"


def test_parser_recognises_email_as_user():
    p = default_principal_parser("analyst@example.com")
    assert p == Principal(PrincipalKind.IDP_USER, "analyst@example.com")


def test_parser_falls_back_to_idp_group_for_bare_name():
    p = default_principal_parser("ANALYSTS_CAD")
    assert p == Principal(PrincipalKind.IDP_GROUP, "ANALYSTS_CAD")


# --- list_in_scope_resources ------------------------------------------------


def test_list_in_scope_resources_returns_config(client_and_stub):
    client, _ = client_and_stub
    trades = _trades()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    assert reader.list_in_scope_resources() == [trades]


# --- auto-expand database scope (with Glue client) -----------------------


@pytest.fixture
def glue_and_stub():
    import boto3 as _b
    from botocore.stub import Stubber as _Stubber
    client = _b.client("glue", region_name="us-east-1")
    stubber = _Stubber(client)
    yield client, stubber
    stubber.deactivate()


def test_database_entry_expands_to_db_plus_all_tables(client_and_stub, glue_and_stub):
    lf_client, _ = client_and_stub
    glue_client, glue_stub = glue_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)

    glue_stub.add_response(
        "get_tables",
        {
            "TableList": [
                {"Name": "trades", "DatabaseName": "finance"},
                {"Name": "positions", "DatabaseName": "finance"},
            ],
        },
        {"CatalogId": CATALOG_ID, "DatabaseName": "finance"},
    )
    glue_stub.activate()

    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[finance],
        glue_client=glue_client,
    )
    expanded = reader.list_in_scope_resources()
    assert expanded == [
        finance,
        ResourceRef(CATALOG_ID, "finance", "trades", None),
        ResourceRef(CATALOG_ID, "finance", "positions", None),
    ]


def test_table_entry_passes_through_unchanged_when_glue_set(client_and_stub, glue_and_stub):
    lf_client, _ = client_and_stub
    glue_client, glue_stub = glue_and_stub
    # No glue stub responses queued: if we accidentally called Glue, this would fail.
    glue_stub.activate()
    trades = _trades()
    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[trades],
        glue_client=glue_client,
    )
    assert reader.list_in_scope_resources() == [trades]


def test_expansion_is_cached(client_and_stub, glue_and_stub):
    """Second call must not re-hit Glue."""
    lf_client, _ = client_and_stub
    glue_client, glue_stub = glue_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)
    # Queue exactly ONE Glue response; a second list_in_scope_resources call
    # would fail if it tried to fetch again.
    glue_stub.add_response(
        "get_tables",
        {"TableList": [{"Name": "trades", "DatabaseName": "finance"}]},
        {"CatalogId": CATALOG_ID, "DatabaseName": "finance"},
    )
    glue_stub.activate()
    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[finance],
        glue_client=glue_client,
    )
    first = reader.list_in_scope_resources()
    second = reader.list_in_scope_resources()
    assert first == second
    glue_stub.assert_no_pending_responses()


def test_get_tables_paginates(client_and_stub, glue_and_stub):
    lf_client, _ = client_and_stub
    glue_client, glue_stub = glue_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)
    glue_stub.add_response(
        "get_tables",
        {"TableList": [{"Name": "trades", "DatabaseName": "finance"}], "NextToken": "n1"},
        {"CatalogId": CATALOG_ID, "DatabaseName": "finance"},
    )
    glue_stub.add_response(
        "get_tables",
        {"TableList": [{"Name": "positions", "DatabaseName": "finance"}]},
        {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "NextToken": "n1"},
    )
    glue_stub.activate()
    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[finance],
        glue_client=glue_client,
    )
    expanded = reader.list_in_scope_resources()
    names = [r.table for r in expanded if r.table is not None]
    assert names == ["trades", "positions"]


def test_mixed_db_and_table_entries_dedup(client_and_stub, glue_and_stub):
    """When the operator lists both a db and one of its tables, the explicit
    table entry must not be duplicated by the expansion."""
    lf_client, _ = client_and_stub
    glue_client, glue_stub = glue_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)
    trades = _trades()
    glue_stub.add_response(
        "get_tables",
        {"TableList": [
            {"Name": "trades", "DatabaseName": "finance"},
            {"Name": "positions", "DatabaseName": "finance"},
        ]},
        {"CatalogId": CATALOG_ID, "DatabaseName": "finance"},
    )
    glue_stub.activate()
    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[trades, finance],
        glue_client=glue_client,
    )
    expanded = reader.list_in_scope_resources()
    # Each ResourceRef appears at most once
    assert len(expanded) == len(set(expanded))
    assert trades in expanded
    assert finance in expanded
    assert ResourceRef(CATALOG_ID, "finance", "positions", None) in expanded


def test_no_glue_client_means_no_expansion(client_and_stub):
    """Back-compat: without a Glue client, db-only entries are returned literally."""
    lf_client, _ = client_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)
    reader = BotoLFReader(
        client=lf_client,
        catalog_id=CATALOG_ID,
        in_scope_resources=[finance],
        glue_client=None,
    )
    assert reader.list_in_scope_resources() == [finance]


# --- list_lf_tag_keys -------------------------------------------------------


def test_list_lf_tag_keys_paginates(client_and_stub):
    client, stubber = client_and_stub
    stubber.add_response(
        "list_lf_tags",
        {
            "LFTags": [{"TagKey": "classification", "TagValues": ["public", "internal"]}],
            "NextToken": "tok1",
        },
        {"CatalogId": CATALOG_ID},
    )
    stubber.add_response(
        "list_lf_tags",
        {"LFTags": [{"TagKey": "lob", "TagValues": ["AE", "PV"]}]},
        {"CatalogId": CATALOG_ID, "NextToken": "tok1"},
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[])
    assert reader.list_lf_tag_keys() == ["classification", "lob"]
    stubber.assert_no_pending_responses()


def test_list_lf_tag_keys_handles_missing_lftags_key(client_and_stub):
    """LF returns no LFTags key when the dictionary is empty in some API versions;
    the reader must treat that as an empty list, not a KeyError."""
    client, stubber = client_and_stub
    stubber.add_response("list_lf_tags", {}, {"CatalogId": CATALOG_ID})
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[])
    assert reader.list_lf_tag_keys() == []


# --- get_lf_tags_on_resource ------------------------------------------------


def test_get_tags_combines_db_and_table_inherited(client_and_stub):
    client, stubber = client_and_stub
    trades = _trades()
    stubber.add_response(
        "get_resource_lf_tags",
        {
            "LFTagOnDatabase": [{"TagKey": "lob", "TagValues": ["AE"]}],
            "LFTagsOnTable": [
                {"TagKey": "classification", "TagValues": ["confidential"]},
            ],
            "LFTagsOnColumns": [],
        },
        {
            "CatalogId": CATALOG_ID,
            "Resource": {
                "Table": {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "Name": "trades"}
            },
            "ShowAssignedLFTags": False,
        },
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    assert set(reader.get_lf_tags_on_resource(trades)) == {
        LFTagAssignment("lob", "AE"),
        LFTagAssignment("classification", "confidential"),
    }


def test_get_tags_explodes_multi_value_tags(client_and_stub):
    client, stubber = client_and_stub
    trades = _trades()
    stubber.add_response(
        "get_resource_lf_tags",
        {
            "LFTagsOnTable": [
                {"TagKey": "classification", "TagValues": ["public", "internal"]},
            ],
        },
        {
            "CatalogId": CATALOG_ID,
            "Resource": {
                "Table": {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "Name": "trades"}
            },
            "ShowAssignedLFTags": False,
        },
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    assert set(reader.get_lf_tags_on_resource(trades)) == {
        LFTagAssignment("classification", "public"),
        LFTagAssignment("classification", "internal"),
    }


def test_get_tags_for_database_level_resource_uses_database_payload(client_and_stub):
    client, stubber = client_and_stub
    finance = ResourceRef(CATALOG_ID, "finance", None, None)
    stubber.add_response(
        "get_resource_lf_tags",
        {"LFTagOnDatabase": [{"TagKey": "lob", "TagValues": ["AE"]}]},
        {
            "CatalogId": CATALOG_ID,
            "Resource": {"Database": {"CatalogId": CATALOG_ID, "Name": "finance"}},
            "ShowAssignedLFTags": False,
        },
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[finance])
    assert reader.get_lf_tags_on_resource(finance) == [LFTagAssignment("lob", "AE")]


# --- list_grants_on_resource ------------------------------------------------


def test_list_grants_aggregates_principal_permissions(client_and_stub):
    client, stubber = client_and_stub
    trades = _trades()
    stubber.add_response(
        "list_permissions",
        {
            "PrincipalResourcePermissions": [
                {
                    "Principal": {"DataLakePrincipalIdentifier": "ANALYSTS_CAD"},
                    "Permissions": ["SELECT"],
                },
                {
                    "Principal": {"DataLakePrincipalIdentifier": "ANALYSTS_CAD"},
                    "Permissions": ["DESCRIBE"],
                },
                {
                    "Principal": {
                        "DataLakePrincipalIdentifier": "arn:aws:iam::123:role/Ghost"
                    },
                    "Permissions": ["SELECT", "INSERT"],
                },
            ],
        },
        {
            "CatalogId": CATALOG_ID,
            "Resource": {
                "Table": {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "Name": "trades"}
            },
        },
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    grants = reader.list_grants_on_resource(trades)
    by_principal = {g.principal: set(g.permissions) for g in grants}
    assert by_principal[Principal(PrincipalKind.IDP_GROUP, "ANALYSTS_CAD")] == {"SELECT", "DESCRIBE"}
    assert by_principal[Principal(PrincipalKind.IAM_ROLE, "Ghost")] == {"SELECT", "INSERT"}


def test_list_grants_paginates(client_and_stub):
    client, stubber = client_and_stub
    trades = _trades()
    resource_payload = {
        "Table": {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "Name": "trades"}
    }
    stubber.add_response(
        "list_permissions",
        {
            "PrincipalResourcePermissions": [
                {
                    "Principal": {"DataLakePrincipalIdentifier": "ANALYSTS_CAD"},
                    "Permissions": ["SELECT"],
                }
            ],
            "NextToken": "n1",
        },
        {"CatalogId": CATALOG_ID, "Resource": resource_payload},
    )
    stubber.add_response(
        "list_permissions",
        {
            "PrincipalResourcePermissions": [
                {
                    "Principal": {"DataLakePrincipalIdentifier": "RISK_CAD"},
                    "Permissions": ["SELECT"],
                }
            ],
        },
        {"CatalogId": CATALOG_ID, "Resource": resource_payload, "NextToken": "n1"},
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    grants = reader.list_grants_on_resource(trades)
    principals = {g.principal.identifier for g in grants}
    assert principals == {"ANALYSTS_CAD", "RISK_CAD"}


def test_list_grants_empty(client_and_stub):
    client, stubber = client_and_stub
    trades = _trades()
    stubber.add_response(
        "list_permissions",
        {"PrincipalResourcePermissions": []},
        {
            "CatalogId": CATALOG_ID,
            "Resource": {
                "Table": {"CatalogId": CATALOG_ID, "DatabaseName": "finance", "Name": "trades"}
            },
        },
    )
    stubber.activate()
    reader = BotoLFReader(client=client, catalog_id=CATALOG_ID, in_scope_resources=[trades])
    assert reader.list_grants_on_resource(trades) == []
