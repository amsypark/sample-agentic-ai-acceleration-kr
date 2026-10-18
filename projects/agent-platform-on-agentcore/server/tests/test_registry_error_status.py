"""Registry API errors keep their meaning on the way out (bst T1 smoke, 2026-10-18).

Every ClientError was a 502. A duplicate name and an edit while the record was
still CREATING are the caller's to retry, not a broken server.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.registry as registry_routes  # noqa: E402


def status_of(code, message="m"):
    with pytest.raises(HTTPException) as err:
        registry_routes._fail(ClientError({"Error": {"Code": code, "Message": message}}, "op"))
    return err.value


def test_a_conflict_is_409_and_says_to_retry_while_the_record_settles():
    out = status_of("ConflictException", "Registry record cannot be modified while in CREATING state.")
    assert out.status_code == 409 and "잠시 후" in out.detail


def test_a_duplicate_name_is_409():
    assert status_of("ConflictException", "A record with name 'x' already exists").status_code == 409


@pytest.mark.parametrize("code,status", [
    ("ResourceNotFoundException", 404),
    ("ValidationException", 400),
    ("ThrottlingException", 503),
    ("AccessDeniedException", 502),
])
def test_other_codes(code, status):
    assert status_of(code).status_code == status
