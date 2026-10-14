# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 계정 레지스트리(0044)의 트리거·제약 — 실 PostgreSQL 에서만 증명된다.

게이트웨이는 ``model_aliases.account_role_arn`` / ``account_external_id`` 사본으로 서명한다.
그 사본을 맞게 유지하는 것은 admin-api 가 아니라 **DB 트리거**다(0044 docstring: 한 작성자만
유지하는 사본은 psql 한 번에 어긋난다). 그래서 이 파일은 ORM 도 서비스도 거치지 않고 SQL 로
직접 쓴다 — 어떤 작성자가 쓰더라도 사본이 레지스트리와 같다는 것이 보증 대상이다.

실패 모드는 예외가 아니라 **틀린 값**이다. 사본이 레지스트리와 어긋나면 게이트웨이는 200 을
돌려주면서 다른 계정으로 청구한다. 그래서 각 경로를 값으로 단정한다.

⚠️ PROOF_DSN 이 alembic head(0044 포함)를 마친 DB 를 가리켜야 한다. 그 DB 를 TEMPLATE 로
   복제한 자기 소유 스크래치 DB(<name>_acctproof) 안에서만 쓴다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_acctproof"
_SYS_USER = "00000000-0000-4000-a000-000000000010"  # db/init seed admin
_INIT_TABLES = Path(__file__).resolve().parents[3] / "db" / "init" / "02_create_tables.sql"

ROLE_A = "arn:aws:iam::111122223333:role/in-region-bedrock"
ROLE_B = "arn:aws:iam::444455556666:role/dedicated-bedrock"

pytestmark = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 트리거·제약은 실 PG 에서만 존재한다(0044 head 필요)."
)


def _split(dsn: str) -> tuple[str, str]:
    pre, _, name = dsn.rpartition("/")
    return pre, name


def _plain(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    # 파괴적 DDL 을 도는 DB 이므로 이름으로 한 번 더 잠근다.
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = await asyncpg.connect(_plain(f"{pre}/postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await admin.execute(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"')
    finally:
        await admin.close()
    yield _plain(f"{pre}/{owned}")
    admin = await asyncpg.connect(_plain(f"{pre}/postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def conn(dsn):
    import asyncpg

    c = await asyncpg.connect(dsn)
    # 테스트마다 깨끗한 상태: 이 파일이 만든 alias/계정만 지운다(시드는 건드리지 않는다).
    await c.execute("DELETE FROM model.model_aliases WHERE alias LIKE 'acctproof-%'")
    await c.execute("DELETE FROM model.aws_accounts WHERE name LIKE 'proof-%'")
    try:
        yield c
    finally:
        await c.close()


async def _account(c, name, role, ext=None):
    await c.execute(
        "INSERT INTO model.aws_accounts (name, display_name, role_arn, external_id, created_by) "
        "VALUES ($1, $1, $2, $3, $4)",
        name, role, ext, _SYS_USER,
    )


async def _alias(c, alias, account_name=None, *, role=None, ext=None, provider="BEDROCK"):
    await c.execute(
        "INSERT INTO model.model_aliases (alias, provider, provider_model_id, api_format, "
        "account_name, account_role_arn, account_external_id, created_by) "
        "VALUES ($1, $2::model.provider, 'anthropic.claude-proof', 'BEDROCK_NATIVE', $3, $4, $5, $6)",
        alias, provider, account_name, role, ext, _SYS_USER,
    )


async def _row(c, alias):
    r = await c.fetchrow(
        "SELECT account_name, account_role_arn, account_external_id, updated_at "
        "FROM model.model_aliases WHERE alias = $1",
        alias,
    )
    return r["account_name"], r["account_role_arn"], r["account_external_id"], r["updated_at"]


async def test_selecting_an_account_fills_the_copy_the_gateway_signs_with(conn):
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a")
    name, role, ext, _ = await _row(conn, "acctproof-1")
    assert (name, role, ext) == ("proof-a", ROLE_A, "ext-a")


async def test_a_forged_copy_next_to_a_selection_is_overwritten_by_the_registry(conn):
    """선택이 있으면 사본은 레지스트리 값이다 — 같이 보낸 다른 ARN 은 남지 않는다."""
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a", role=ROLE_B, ext="forged")
    assert (await _row(conn, "acctproof-1"))[:3] == ("proof-a", ROLE_A, "ext-a")


async def test_a_registry_edit_repoints_every_selecting_alias_and_stamps_it(conn):
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a")
    await _alias(conn, "acctproof-2", "proof-a")
    await _alias(conn, "acctproof-3")  # 선택 없음 — 건드려지면 안 된다
    before = {a: (await _row(conn, a))[3] for a in ("acctproof-1", "acctproof-2", "acctproof-3")}

    await conn.execute(
        "UPDATE model.aws_accounts SET role_arn = $1, external_id = NULL WHERE name = 'proof-a'",
        ROLE_B,
    )
    for a in ("acctproof-1", "acctproof-2"):
        name, role, ext, stamp = await _row(conn, a)
        assert (name, role, ext) == ("proof-a", ROLE_B, None), a
        assert stamp > before[a], f"{a}: 청구 계정이 바뀐 시각이 updated_at 에 남아야 한다"
    assert await _row(conn, "acctproof-3") == (None, None, None, before["acctproof-3"])


async def test_a_same_value_save_does_not_restamp_the_aliases(conn):
    """콘솔은 폼 전체를 다시 보낸다. 값이 같은 저장으로 updated_at 이 움직이면 그 칼럼이
    '청구 계정이 바뀐 시각' 이라는 뜻을 잃는다."""
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a")
    t0 = (await _row(conn, "acctproof-1"))[3]
    await conn.execute(
        "UPDATE model.aws_accounts SET role_arn = role_arn, external_id = external_id, "
        "display_name = 'renamed' WHERE name = 'proof-a'"
    )
    assert (await _row(conn, "acctproof-1"))[3] == t0


async def test_clearing_the_selection_clears_the_copy(conn):
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a")
    await conn.execute(
        "UPDATE model.model_aliases SET account_name = NULL WHERE alias = 'acctproof-1'"
    )
    assert (await _row(conn, "acctproof-1"))[:3] == (None, None, None)


async def test_an_unrelated_alias_edit_keeps_the_copy_exact(conn):
    await _account(conn, "proof-a", ROLE_A, "ext-a")
    await _alias(conn, "acctproof-1", "proof-a")
    await conn.execute(
        "UPDATE model.model_aliases SET description = 'x' WHERE alias = 'acctproof-1'"
    )
    assert (await _row(conn, "acctproof-1"))[:3] == ("proof-a", ROLE_A, "ext-a")


async def test_a_copy_without_a_selection_is_refused_not_kept(conn):
    """선택 없이 사본만 쓰면(psql, 구 작성자) 레지스트리가 모르는 계정이 생긴다 — 거부한다."""
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError, match="ck_model_aliases_account_materialized"):
        await _alias(conn, "acctproof-1", role=ROLE_A)
    await _alias(conn, "acctproof-2")
    with pytest.raises(asyncpg.CheckViolationError, match="ck_model_aliases_account_materialized"):
        await conn.execute(
            "UPDATE model.model_aliases SET account_role_arn = $1 WHERE alias = 'acctproof-2'",
            ROLE_A,
        )


async def test_an_external_id_without_a_role_is_refused(conn):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await _alias(conn, "acctproof-1", ext="lonely")


async def test_an_unknown_account_name_is_refused_with_the_name(conn):
    import asyncpg

    with pytest.raises(asyncpg.ForeignKeyViolationError, match="proof-missing"):
        await _alias(conn, "acctproof-1", "proof-missing")


async def test_deleting_a_selected_account_is_restricted(conn):
    """SET NULL 이면 그 alias 의 청구가 조용히 기본 계정으로 옮겨 간다."""
    import asyncpg

    await _account(conn, "proof-a", ROLE_A)
    await _account(conn, "proof-b", ROLE_B)
    await _alias(conn, "acctproof-1", "proof-a")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("DELETE FROM model.aws_accounts WHERE name = 'proof-a'")
    await conn.execute("DELETE FROM model.aws_accounts WHERE name = 'proof-b'")  # 미사용은 지워진다
    assert await conn.fetchval("SELECT count(*) FROM model.aws_accounts WHERE name = 'proof-b'") == 0


async def test_one_role_is_one_registry_entry(conn):
    import asyncpg

    await _account(conn, "proof-a", ROLE_A, "ext-a")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _account(conn, "proof-b", ROLE_A, "ext-other")


@pytest.mark.parametrize(
    "name", ["default", "gateway", "inherit", "none", "ab", "-proof", "proof-", "Proof-a", "proof_a"]
)
async def test_reserved_and_malformed_names_are_refused(conn, name):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await _account(conn, name, ROLE_A)


@pytest.mark.parametrize(
    "role",
    [
        "arn:aws:iam::111122223333:user/someone",  # 사용자 ARN — assume 할 수 없다
        "111122223333",  # 계정 번호만
        "arn:aws:iam::12345:role/short-account",  # 12자리 아님
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude",  # Bedrock ARN
    ],
)
async def test_only_iam_role_arns_are_accepted(conn, role):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await _account(conn, "proof-a", role)


async def test_other_partitions_are_accepted(conn):
    await _account(conn, "proof-a", "arn:aws-us-gov:iam::111122223333:role/gov")
    assert await conn.fetchval("SELECT count(*) FROM model.aws_accounts WHERE name = 'proof-a'") == 1


async def test_a_one_character_external_id_is_refused(conn):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await _account(conn, "proof-a", ROLE_A, "x")


async def test_rerunning_the_init_sql_keeps_the_triggers_working(conn):
    """클라우드 마이그레이션 Job 은 매번 init SQL 을 alembic 보다 먼저 다시 돌린다.

    재실행이 트리거를 지웠다 다시 만들면(DROP→CREATE) 그 사이의 레지스트리 수정이 전파되지
    않는다. init SQL 은 CREATE OR REPLACE TRIGGER 를 쓴다 — 재실행 뒤에도 전파가 그대로 되는지
    확인한다(그리고 재실행 자체가 head 스키마 위에서 실패하지 않는지).
    """
    await conn.execute(_INIT_TABLES.read_text(encoding="utf-8"))
    await _account(conn, "proof-a", ROLE_A)
    await _alias(conn, "acctproof-1", "proof-a")
    await conn.execute(
        "UPDATE model.aws_accounts SET role_arn = $1 WHERE name = 'proof-a'", ROLE_B
    )
    assert (await _row(conn, "acctproof-1"))[:3] == ("proof-a", ROLE_B, None)
    n = await conn.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname IN "
        "('trg_alias_account_materialize', 'trg_aws_account_propagate')"
    )
    assert n == 2
    validated = await conn.fetchval(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_model_aliases_account_materialized'"
    )
    assert validated is True, "재실행이 0044 가 검증한 CHECK 를 NOT VALID 로 되돌리면 안 된다"
