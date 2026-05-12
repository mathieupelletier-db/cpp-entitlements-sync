"""Tests for the LF -> UC level-aware privilege mapping."""
import pytest

from entitlements_sync.models import ResourceRef
from entitlements_sync.privilege_mapping import map_lf_to_uc_privileges


def _catalog():
    return ResourceRef("main", "", None, None)


def _schema():
    return ResourceRef("main", "finance", None, None)


def _table():
    return ResourceRef("main", "finance", "trades", None)


# --- DESCRIBE: the bug that triggered this module ----------------------------


def test_describe_at_catalog_becomes_use_catalog():
    uc, unsupported = map_lf_to_uc_privileges(("DESCRIBE",), _catalog())
    assert uc == {"USE CATALOG"}
    assert unsupported == []


def test_describe_at_schema_becomes_use_schema():
    uc, unsupported = map_lf_to_uc_privileges(("DESCRIBE",), _schema())
    assert uc == {"USE SCHEMA"}
    assert unsupported == []


def test_describe_at_table_is_dropped_as_unsupported():
    """SELECT implies metadata access on a table — UC has no separate
    table-level DESCRIBE. The LF DESCRIBE-on-table fails on real UC, so the
    mapping must drop it rather than emit invalid SQL."""
    uc, unsupported = map_lf_to_uc_privileges(("DESCRIBE",), _table())
    assert uc == set()
    assert unsupported == ["DESCRIBE"]


# --- SELECT ------------------------------------------------------------------


def test_select_on_table_passes_through():
    uc, _ = map_lf_to_uc_privileges(("SELECT",), _table())
    assert uc == {"SELECT"}


def test_select_at_schema_or_catalog_is_unsupported():
    for resource in (_schema(), _catalog()):
        uc, unsupported = map_lf_to_uc_privileges(("SELECT",), resource)
        assert uc == set()
        assert unsupported == ["SELECT"]


# --- writes fold to MODIFY ---------------------------------------------------


def test_writes_fold_to_modify_at_table():
    uc, _ = map_lf_to_uc_privileges(("ALTER", "INSERT", "DELETE"), _table())
    assert uc == {"MODIFY"}  # all three collapse to one privilege


def test_writes_dedup_when_already_present():
    uc, _ = map_lf_to_uc_privileges(("INSERT", "INSERT", "DELETE"), _table())
    assert uc == {"MODIFY"}


# --- DROP ----------------------------------------------------------------------


def test_drop_is_always_unsupported():
    """UC requires ownership for DROP; no grantable equivalent at any level."""
    for resource in (_catalog(), _schema(), _table()):
        uc, unsupported = map_lf_to_uc_privileges(("DROP",), resource)
        assert uc == set()
        assert unsupported == ["DROP"]


# --- ALL --------------------------------------------------------------------


def test_all_maps_to_all_privileges_at_any_level():
    for resource in (_catalog(), _schema(), _table()):
        uc, _ = map_lf_to_uc_privileges(("ALL",), resource)
        assert uc == {"ALL PRIVILEGES"}


# --- Creates ---------------------------------------------------------------


def test_create_table_at_schema_maps_to_create_table():
    uc, _ = map_lf_to_uc_privileges(("CREATE_TABLE",), _schema())
    assert uc == {"CREATE TABLE"}


def test_create_database_at_catalog_maps_to_create_schema():
    uc, _ = map_lf_to_uc_privileges(("CREATE_DATABASE",), _catalog())
    assert uc == {"CREATE SCHEMA"}


# --- Unknown / mixed ---------------------------------------------------------


def test_unknown_lf_permission_is_unsupported():
    uc, unsupported = map_lf_to_uc_privileges(("MAGIC",), _table())
    assert uc == set()
    assert unsupported == ["MAGIC"]


def test_real_cpp_admin_grants_at_schema_level():
    """The CPP admin role has ['ALL', 'ALTER', 'CREATE_TABLE', 'DESCRIBE', 'DROP']
    on the database (schema in UC terms). Verify the mapping produces sane SQL."""
    uc, unsupported = map_lf_to_uc_privileges(
        ("ALL", "ALTER", "CREATE_TABLE", "DESCRIBE", "DROP"), _schema()
    )
    # ALL -> ALL PRIVILEGES (covers the others)
    # CREATE_TABLE -> CREATE TABLE
    # DESCRIBE -> USE SCHEMA
    # ALTER, DROP -> unsupported at schema level
    assert uc == {"ALL PRIVILEGES", "CREATE TABLE", "USE SCHEMA"}
    assert set(unsupported) == {"ALTER", "DROP"}


def test_real_cpp_analyst_grant_on_table():
    """Data analyst on a TABLE: ['DESCRIBE', 'SELECT']. SELECT covers both."""
    uc, unsupported = map_lf_to_uc_privileges(("DESCRIBE", "SELECT"), _table())
    assert uc == {"SELECT"}
    assert unsupported == ["DESCRIBE"]


def test_real_cpp_analyst_grant_on_database():
    """Data analyst on a DATABASE: ['DESCRIBE']. Maps to USE SCHEMA in UC."""
    uc, _ = map_lf_to_uc_privileges(("DESCRIBE",), _schema())
    assert uc == {"USE SCHEMA"}


def test_empty_input_returns_empty_output():
    uc, unsupported = map_lf_to_uc_privileges((), _table())
    assert uc == set()
    assert unsupported == []
