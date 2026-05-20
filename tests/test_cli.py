"""Tests for entitlements_sync.cli — focused on AWS credential resolution.

build_components itself is hard to unit-test in full (it constructs real
boto3 + databricks-sdk objects), so we cover the credential-resolution
branch via the smaller _build_aws_session helper, mocking the WorkspaceClient's
low-level api_client.do (the SDK REST entry, stable across SDK versions).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from entitlements_sync.cli import _build_aws_session

_TEMP_CREDS_PATH = "/api/2.1/unity-catalog/temporary-service-credentials"


def test_build_aws_session_default_chain_when_no_service_credential():
    """No service_credential_name -> boto3 default chain, region passed through."""
    ws = MagicMock()
    aws_cfg = {"region": "us-west-2", "catalog_id": "123456789012"}

    session = _build_aws_session(ws, aws_cfg)

    assert session.region_name == "us-west-2"
    # Default chain means we never hit the credentials REST endpoint
    ws.api_client.do.assert_not_called()


def test_build_aws_session_uses_service_credential_when_named():
    """service_credential_name set -> session built from temp STS creds
    returned by the UC temporary-service-credentials REST endpoint."""
    ws = MagicMock()
    ws.api_client.do.return_value = {
        "aws_temp_credentials": {
            "access_key_id": "ASIAFAKE",
            "secret_access_key": "secretFake",
            "session_token": "tokenFake",
        }
    }
    aws_cfg = {
        "region": "us-west-2",
        "catalog_id": "123456789012",
        "service_credential_name": "my_credential",
    }

    session = _build_aws_session(ws, aws_cfg)

    ws.api_client.do.assert_called_once_with(
        "POST",
        _TEMP_CREDS_PATH,
        body={"credential_name": "my_credential"},
    )
    assert session.region_name == "us-west-2"
    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "ASIAFAKE"
    assert frozen.secret_key == "secretFake"
    assert frozen.token == "tokenFake"


def test_build_aws_session_empty_string_service_credential_falls_back_to_default():
    """A literal empty string in the config should behave the same as omission,
    not as 'use a credential named ""'."""
    ws = MagicMock()
    aws_cfg = {
        "region": "us-east-1",
        "catalog_id": "123",
        "service_credential_name": "",
    }

    session = _build_aws_session(ws, aws_cfg)

    assert session.region_name == "us-east-1"
    ws.api_client.do.assert_not_called()
