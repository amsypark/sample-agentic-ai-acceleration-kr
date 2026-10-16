# Copyright 2026 © Amazon.com and Affiliates.

"""워커가 서빙 계정(aws_account_id)을 usage_logs 에 실제로 쓰는지 (실 PostgreSQL, 0045).

게이트웨이 effective_account_id 가 판정한 계정이 cost:stream 을 타고 와서
usage.usage_logs.aws_account_id 에 그대로 남아야 정산이 그 행의 bedrock_request_id 를 맞는
계정의 invocation log 에서 찾는다. INSERT 컬럼 목록·파라미터·스키마 중 하나가 어긋나면 값이
조용히 NULL 이 된다 — 실 행을 읽어 막는다.

⚠️ INSERT 가 aws_account_id 를 **명시 컬럼**으로 이름 대므로 0045(또는 같은 내용의 init SQL)가
   워커보다 먼저 적용돼야 한다. PROOF_DSN 은 head(0045 포함)를 가리킨다. 그 DB 를 TEMPLATE 로
   복제한 자기 소유 DB(<name>_acctidproof) 안에서만 쓴다.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_acctidproof"
USER_ID = "95111111-1111-1111-1111-111111111111"
TEAM_ID = "95222222-2222-2222-2222-222222222222"
DEPT_ID = "95333333-3333-3333-3333-333333333333"
ORG_ID = "95444444-4444-4444-4444-444444444444"

_pg = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG 필요(0045 head).")


def _entry(rid, account):
    from worker.schemas.cost_stream import CostStreamEntry

    return CostStreamEntry(
        request_id=rid, user_id=USER_ID, team_id=TEAM_ID, dept_id=DEPT_ID,
        model_alias="claude-sonnet-5-5", provider="BEDROCK",
        input_tokens=10, output_tokens=5, cost_usd=Decimal("0.0001"),
        latency_ms=100, aws_account_id=account,
        requested_at="2026-06-01T00:00:00+00:00", completed_at="2026-06-01T00:00:01+00:00",
        period="2026-06", date="2026-06-01",
    )


def _split(dsn):
    pre, _, name = dsn.rpartition("/")
    return pre, name


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    plain = f"{pre}/postgres".replace("postgresql+asyncpg://", "postgresql://", 1)
    admin = await asyncpg.connect(plain)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
        await admin.execute(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"')
    finally:
        await admin.close()
    url = f"{pre}/{owned}"
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute(
            "INSERT INTO auth.organizations (id,name) VALUES ($1,'acct-org') "
            "ON CONFLICT DO NOTHING", ORG_ID)
        await conn.execute(
            "INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'acct-d') "
            "ON CONFLICT DO NOTHING", DEPT_ID, ORG_ID)
        await conn.execute(
            "INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'acct-t') "
            "ON CONFLICT DO NOTHING", TEAM_ID, DEPT_ID)
        await conn.execute(
            "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
            "VALUES ($1,$2,'acct@example.invalid','acct','ADMIN','acct-sub') "
            "ON CONFLICT DO NOTHING", USER_ID, TEAM_ID)
    finally:
        await conn.close()
    yield url
    admin = await asyncpg.connect(plain)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
    finally:
        await admin.close()


async def _flush(engine, entries):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from worker.batch_flusher import BatchFlusher

    class _Pipe:
        def __getattr__(self, _n):
            return lambda *a, **k: None

        async def execute(self):
            return None

    class _Redis:
        def pipeline(self, *a, **k):
            return _Pipe()

        async def publish(self, *a, **k):
            return None

    fl = BatchFlusher(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), redis=_Redis()
    )
    await fl.flush(entries)


async def _read(engine, ids):
    from sqlalchemy import text

    async with engine.connect() as c:
        return dict((await c.execute(text(
            "SELECT request_id, aws_account_id FROM usage.usage_logs WHERE request_id = ANY(:i)"),
            {"i": list(ids)})).all())


@_pg
async def test_the_serving_account_is_persisted_verbatim(dsn):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        rid = f"acct-{uuid.uuid4()}"
        await _flush(engine, [_entry(rid, "111122223333")])
        assert (await _read(engine, [rid])) == {rid: "111122223333"}
    finally:
        await engine.dispose()


@_pg
async def test_the_gateway_account_is_persisted_as_null(dsn):
    """대조군. None(게이트웨이 자신의 계정)은 NULL 로 남는다 — 다른 값으로 채워지면 안 된다."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        rid = f"none-{uuid.uuid4()}"
        await _flush(engine, [_entry(rid, None)])
        assert (await _read(engine, [rid])) == {rid: None}
    finally:
        await engine.dispose()


@_pg
async def test_a_malformed_account_does_not_cost_the_batch(dsn):
    """형식이 어긋난 값이 섞여도 같은 배치의 다른 행까지 함께 기록된다(그 값만 NULL)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        good, bad = f"good-{uuid.uuid4()}", f"bad-{uuid.uuid4()}"
        await _flush(engine, [_entry(good, "444455556666"), _entry(bad, "not-an-account-id")])
        assert (await _read(engine, [good, bad])) == {good: "444455556666", bad: None}
    finally:
        await engine.dispose()
