# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""0046 — GLOBAL rate limit 을 모델별로. 마이그레이션 함수·init SQL 을 실 PostgreSQL 에서 고정한다.

  1. ``db/init`` 이 만드는 rate-limit 인덱스·``rl_global_release_guard`` 트리거·함수와 0046 ``upgrade()`` 가
     만드는 것이 글자까지 같다(``pg_get_indexdef`` / ``pg_get_triggerdef`` / ``pg_get_functiondef``). init SQL
     은 매 마이그레이션 Job 에서 alembic 보다 먼저 돌고 compose·CI 는 init SQL 로 스키마를 만든다.
  2. 여러 모델이 실제 GLOBAL 한도를 가지면 ``downgrade()`` 는 그 목록과 정리 방법을 담아 실패하고 아무것도
     바꾸지 않는다(대조군: 판정이 없으면 0045 인덱스 생성이 PostgreSQL 의 중복 키 오류로 죽는다).
  3. 아무것도 집행하지 않는 행 — rpm·tpm 이 모두 NULL 이거나 alias 가 없는 행 — 은 판정에서 빠지고, 트리거를
     ``SET LOCAL`` 로 우회해 비활성화되며 감사 로그가 남는다(대조군: alias 없는 행을 세던 판정은 거부했다).
  4. 0046 은 양방향 모두 ACCESS EXCLUSIVE 를 **먼저** 잡는다 — 이 테이블을 읽고 이어서 쓰는 트랜잭션이
     끝까지 간다(대조군: SHARE·SHARE ROW EXCLUSIVE 에서 올리던 순서는 그 트랜잭션을 교착으로 죽였다).
  5. init SQL 의 0046 블록은 객체가 있으면 잠그지 않고, 만들어야 할 때만 잠그며 그 대기는 lock_timeout 5s 로
     묶인다 — 넘으면 깨끗이 실패하고 다시 돌리면 된다(대조군: 맨 ``CREATE UNIQUE INDEX IF NOT EXISTS`` 는
     인덱스가 있어도 열린 쓰기 뒤에서 기다린다).
  6. 0046 이 USER/TEAM 인덱스의 **이름을 그대로** 쓰는 이유: 이전 차트의 init SQL 이 0046 DB 에서 다시 돌아도
     no-op 이다(대조군: 그 이름이 없으면 같은 문장이 단일 슬롯을 조용히 되살린다).

마이그레이션 함수는 alembic 없이 돈다(이 서비스의 lock 에 alembic 이 없다). 모듈의 ``from alembic import op``
자리에 ``execute`` / ``get_bind`` 만 가진 대역을 넣고, alembic 의 ``transaction_per_migration`` 처럼 한
트랜잭션에서 실행한다 — 예외면 롤백된다. 문자열 SQL 은 alembic 처럼 ``text()`` 로 감싼다. 실 alembic CLI
왕복은 CI 의 pg-proof 잡이 별도 DB 에서 돈다.

이전 릴리스 admin-api 의 저장(alias 무관 upsert)이 트리거에 막히는 것은
tests/regression/test_high_duplicate_active_config.py 가 실제 저장 경로로 본다.

⚠️ ``PROOF_DSN`` 의 DB 는 건드리지 않는다. 자기 소유 ``<dbname>_rl0046`` 을 init SQL 로 만들어 쓰고 지운다.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

from tests.regression.test_high_console_contract_real_db import (
    SEED_ADMIN_ID,
    _apply_init_sql,
    _split_dsn,
)
from tests.regression.test_high_duplicate_active_config import _executed_0024_index_sql

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_rl0046"
_PROJECT = Path(__file__).resolve().parents[3]
_M0046 = _PROJECT / "db" / "versions" / "0046_global_rate_limit_per_model.py"
_INIT_TABLES = _PROJECT / "db" / "init" / "02_create_tables.sql"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

_UNIQUE_INDEXES = (
    "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
    "WHERE indrelid = 'model.rate_limit_configs'::regclass AND indisunique AND NOT indisprimary "
    "ORDER BY 1"
)
_GUARD = (
    "SELECT 'trigger', pg_get_triggerdef(oid) FROM pg_trigger "
    "WHERE tgrelid = 'model.rate_limit_configs'::regclass AND NOT tgisinternal "
    "UNION ALL "
    "SELECT 'function', pg_get_functiondef(p.oid) FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'model' AND p.proname = 'rl_global_release_guard' "
    "ORDER BY 1"
)
_ACTIVE_GLOBAL = (
    "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
    "WHERE scope = 'GLOBAL' AND is_active ORDER BY model_alias NULLS FIRST"
)
_AUDITED_RELEASES = (
    "SELECT changes -> 'before' ->> 'model_alias', changes -> 'before' ->> 'rpm' FROM audit.audit_logs "
    "WHERE action = 'DELETE_RATE_LIMIT' AND resource_type = 'RateLimitConfig' "
    "ORDER BY 1 NULLS FIRST"
)


class _Op:
    """``alembic.op`` 중 0046 이 쓰는 두 메서드 — 넘겨받은 연결(=그 마이그레이션의 트랜잭션)에서 실행한다."""

    def __init__(self) -> None:
        self.bind = None

    def execute(self, sql) -> None:
        from sqlalchemy import text

        self.bind.execute(text(sql) if isinstance(sql, str) else sql)

    def get_bind(self):
        return self.bind


@pytest.fixture(scope="module")
def m0046():
    op = _Op()
    fake = types.ModuleType("alembic")
    fake.op = op
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec = importlib.util.spec_from_file_location("_m0046_under_test", _M0046)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = saved
    assert (module.revision, module.down_revision) == ("0046", "0045")
    module._test_op = op
    return module


async def _run(engine, module, func: str, pid: dict | None = None) -> None:
    """alembic 의 transaction_per_migration 처럼 함수 하나를 트랜잭션 하나로 돌린다(예외면 롤백).
    ``pid`` 를 주면 그 트랜잭션의 backend pid 를 적어 둔다(잠금 대기 관찰용)."""

    def _call(sync_conn) -> None:
        if pid is not None:
            pid["pid"] = sync_conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        module._test_op.bind = sync_conn
        getattr(module, func)()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _admin_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _ = _split_dsn(PROOF_DSN)
    return create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")


def _plain(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
async def dsn():
    """테스트마다 init SQL 만으로 새 DB — 마이그레이션 함수가 스키마를 바꾸므로 공유하지 않는다."""
    from sqlalchemy import text

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_SUFFIX}"
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    await _apply_init_sql(f"{prefix}/{owned}")
    yield f"{prefix}/{owned}"
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture
async def engine(dsn):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(dsn)
    yield eng
    await eng.dispose()


async def _all(engine, sql: str) -> list[tuple]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def _state(engine) -> tuple:
    return await _all(engine, _UNIQUE_INDEXES), await _all(engine, _GUARD)


async def _global(engine, alias: str | None, *, rpm=None, tpm=None) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO model.rate_limit_configs "
                "(scope, scope_id, model_alias, rpm_limit, tpm_limit, is_active, created_by) "
                "VALUES ('GLOBAL', NULL, :a, :rpm, :tpm, true, :by)"
            ),
            {"a": alias, "rpm": rpm, "tpm": tpm, "by": SEED_ADMIN_ID},
        )


# ── 1. init SQL = upgrade() ─────────────────────────────────────────────────────────────


async def test_init_sql_and_the_migration_build_the_same_schema(engine, m0046):
    init_only = await _state(engine)
    assert [name for name, _ in init_only[0]] == [
        "model.uq_rate_limit_configs_active",
        "model.uq_rate_limit_configs_global_active",
    ]
    assert [kind for kind, _ in init_only[1]] == ["function", "trigger"]
    assert "DEFERRABLE INITIALLY DEFERRED" in init_only[1][1][1]

    await _run(engine, m0046, "downgrade")  # GLOBAL 행이 없다 → 통과
    indexes, guard = await _state(engine)
    assert [name for name, _ in indexes] == ["model.uq_rate_limit_configs_active"]
    assert guard == [], "downgrade 가 트리거와 함수를 지워야 한다"
    assert indexes != init_only[0][:1], "대조군 — 비교가 정의 차이를 실제로 잡는다"

    await _run(engine, m0046, "upgrade")
    assert await _state(engine) == init_only


# ── 2·3. downgrade 판정 ──────────────────────────────────────────────────────────────────


async def test_downgrade_refuses_while_two_models_hold_a_limit(engine, m0046):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6", tpm=200000)
    await _global(engine, "claude-sonnet-4-6[1m]")  # 비운 행 — 혼자서는 막지 않는다
    await _global(engine, None, rpm=5)  # alias 없는 행 — 집행되지 않으므로 판정에서 빠진다
    before = (await _state(engine), await _all(engine, _ACTIVE_GLOBAL))

    with pytest.raises(RuntimeError) as refused:
        await _run(engine, m0046, "downgrade")
    message = str(refused.value)
    assert "claude-opus-4-7: rpm=60, tpm=None" in message
    assert "claude-sonnet-4-6: rpm=None, tpm=200000" in message
    assert "2 models have one" in message
    assert "claude-sonnet-4-6[1m]" not in message and "None: rpm" not in message
    assert "SET LOCAL rl.allow_global_release = 'on';" in message
    assert "updated_at = now()" in message and "Nothing was changed" in message
    # 트랜잭션이 롤백됐다 — 비활성화도 감사 기록도 남지 않았다.
    assert (await _state(engine), await _all(engine, _ACTIVE_GLOBAL)) == before
    assert await _all(engine, _AUDITED_RELEASES) == []

    # 대조군 — 판정이 없으면 같은 데이터에서 0045 인덱스가 PostgreSQL 의 중복 키 오류로 죽는다.
    with pytest.raises(IntegrityError, match="could not create unique index"):
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
            await conn.execute(text(m0046.CREATE_SINGLE_SLOT_INDEX))


async def test_downgrade_releases_unenforced_rows_with_the_bypass_and_audits_them(
    engine, m0046, capsys
):
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6")  # 비운 행 — 아무것도 집행하지 않는다
    await _global(engine, None, rpm=5)  # alias 없는 행 — 게이트웨이는 GLOBAL 을 alias 로만 읽는다
    # 대조군 — alias 없는 행까지 세던 판정(리뷰 전)에는 실제 한도가 둘이라 이 상태를 거부했다.
    limited = [r for r in await _all(engine, _ACTIVE_GLOBAL) if r[1] is not None or r[2] is not None]
    assert len(limited) == 2

    await _run(engine, m0046, "downgrade")
    printed = capsys.readouterr().out
    assert "deactivated the active GLOBAL row of claude-sonnet-4-6 (rpm=None, tpm=None)" in printed
    assert "deactivated the active GLOBAL row of <no alias> (rpm=5, tpm=None)" in printed
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]
    assert await _all(engine, _AUDITED_RELEASES) == [(None, "5"), ("claude-sonnet-4-6", None)]
    assert (await _state(engine))[1] == [], "트리거·함수가 남았다"

    # 대조군 — 0045 의 단일 슬롯이 돌아왔다: 두 번째 모델의 GLOBAL 행이 거부된다.
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)

    await _run(engine, m0046, "upgrade")
    await _global(engine, "claude-sonnet-4-6", tpm=100000)
    assert await _all(engine, _ACTIVE_GLOBAL) == [
        ("claude-opus-4-7", 60, None),
        ("claude-sonnet-4-6", None, 100000),
    ]
    assert [kind for kind, _ in (await _state(engine))[1]] == ["function", "trigger"]


async def test_control_without_set_constraints_immediate_the_downgrade_ddl_is_refused(engine, m0046):
    """downgrade 가 비활성화한 행의 지연 트리거 이벤트를 먼저 처리하지 않으면, 같은 트랜잭션의
    ``CREATE INDEX`` 를 PostgreSQL 이 거부한다 — ``SET CONSTRAINTS ALL IMMEDIATE`` 가 필요한 이유."""
    from sqlalchemy.exc import DBAPIError

    await _global(engine, "claude-sonnet-4-6")  # 비운 행 — downgrade 가 비활성화한다
    op = m0046._test_op
    real_execute = op.execute
    op.execute = lambda sql: None if sql == "SET CONSTRAINTS ALL IMMEDIATE" else real_execute(sql)
    try:
        with pytest.raises(DBAPIError, match="pending trigger events"):
            await _run(engine, m0046, "downgrade")
    finally:
        op.execute = real_execute
    assert [kind for kind, _ in (await _state(engine))[1]] == ["function", "trigger"]


# ── 5. 잠금 순서 ──────────────────────────────────────────────────────────────────────────

# 리뷰 전 순서 — upgrade 는 SHARE(CREATE INDEX)에서, downgrade 는 SHARE ROW EXCLUSIVE 에서 시작해 DROP INDEX
# 에서 ACCESS EXCLUSIVE 로 올렸다. 대조군으로만 쓴다.
_PREVIOUS_ORDER = {
    "upgrade": [
        "SET LOCAL lock_timeout = '5s'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
        "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
        "WHERE is_active = true AND scope = 'GLOBAL'",
        "DROP INDEX IF EXISTS model.uq_rate_limit_configs_active",
    ],
    "downgrade": [
        "SET LOCAL lock_timeout = '5s'",
        "LOCK TABLE model.rate_limit_configs IN SHARE ROW EXCLUSIVE MODE",
        "SELECT count(*) FROM model.rate_limit_configs WHERE scope = 'GLOBAL' AND is_active",
        "DROP INDEX IF EXISTS model.uq_rate_limit_configs_global_active",
    ],
}


async def _read_then_write_during(dsn, migrate) -> dict:
    """T 가 테이블을 읽은 채로 ``migrate`` 를 시작시키고, 그것이 잠금을 기다리는 동안 T 가 쓰고 커밋한다.
    그동안 새 읽기 하나가 얼마나 기다리는지도 잰다."""
    import asyncpg

    t, reader, observer = [await asyncpg.connect(_plain(dsn)) for _ in range(3)]
    out: dict = {}
    pid: dict = {}
    try:
        tx = t.transaction()
        await tx.start()
        await t.fetchval("SELECT count(*) FROM model.rate_limit_configs")  # T 가 읽었다

        async def _migrate():
            try:
                await migrate(pid)
                out["migration"] = "ok"
            except Exception as e:  # noqa: BLE001 — 결과로 비교한다
                out["migration"] = type(e).__name__

        task = asyncio.ensure_future(_migrate())
        for _ in range(250):
            if "pid" in pid and await observer.fetchval(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid["pid"]
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("마이그레이션이 잠금을 기다리지 않았다 — 시나리오가 성립하지 않는다")

        async def _read():
            t0 = time.monotonic()
            await reader.fetchval("SELECT count(*) FROM model.rate_limit_configs")
            out["reader_waited_s"] = time.monotonic() - t0

        read = asyncio.ensure_future(_read())
        await asyncio.sleep(0.1)
        try:
            await t.execute(
                "INSERT INTO model.rate_limit_configs (scope, scope_id, rpm_limit, is_active, created_by) "
                "VALUES ('USER', gen_random_uuid(), 1, true, $1)",
                SEED_ADMIN_ID,
            )
            await asyncio.sleep(0.3)
            await tx.commit()
            out["T"] = "ok"
        except Exception as e:  # noqa: BLE001
            await tx.rollback()
            out["T"] = type(e).__name__
        await asyncio.wait_for(task, 20)
        await asyncio.wait_for(read, 20)
    finally:
        for c in (t, reader, observer):
            await c.close()
    return out


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_a_read_then_write_transaction_survives_0046(dsn, engine, m0046, direction):
    if direction == "upgrade":
        await _run(engine, m0046, "downgrade")  # 0045 상태에서 출발한다

    out = await _read_then_write_during(dsn, lambda pid: _run(engine, m0046, direction, pid))
    assert out["T"] == "ok" and out["migration"] == "ok", out
    # 읽기는 대기 중인 ACCESS EXCLUSIVE 뒤에 줄을 선다 — T 가 끝나고 DDL 이 끝날 때까지(lock_timeout 이 상한).
    assert out["reader_waited_s"] < 5, out


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_control_the_previous_lock_order_deadlocks(dsn, engine, m0046, direction):
    import asyncpg

    if direction == "upgrade":
        await _run(engine, m0046, "downgrade")

    async def _previous(pid: dict) -> None:
        conn = await asyncpg.connect(_plain(dsn))
        try:
            pid["pid"] = conn.get_server_pid()
            async with conn.transaction():
                for statement in _PREVIOUS_ORDER[direction]:
                    await conn.execute(statement)
                raise _Rollback  # 대조군은 스키마를 바꾸지 않는다
        except _Rollback:
            pass
        finally:
            await conn.close()

    out = await _read_then_write_during(dsn, _previous)
    assert "DeadlockDetectedError" in (out["T"], out["migration"]), out


class _Rollback(Exception):
    pass


# ── 6. init SQL 의 0046 블록 ─────────────────────────────────────────────────────────────


def _init_sql_0046_block() -> str:
    """02_create_tables.sql 의 0046 객체 블록(DO $$ … $$;)."""
    src = _INIT_TABLES.read_text(encoding="utf-8")
    match = re.search(r"DO \$\$\nDECLARE\n    prev_lock_timeout text.*?\nEND\n\$\$;", src, re.S)
    assert match and "rl_global_release_guard" in match.group(0), "init SQL 의 0046 블록을 못 찾았다"
    return match.group(0)


async def _open_writer(dsn):
    """rate_limit_configs 에 쓰고 커밋하지 않은 트랜잭션 — ROW EXCLUSIVE 를 쥐고 있다."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    tx = conn.transaction()
    await tx.start()
    await conn.execute(
        "INSERT INTO model.rate_limit_configs (scope, scope_id, rpm_limit, is_active, created_by) "
        "VALUES ('USER', gen_random_uuid(), 1, true, $1)",
        SEED_ADMIN_ID,
    )
    return conn, tx


async def test_init_sql_block_takes_no_lock_when_the_objects_exist(dsn):
    import asyncpg

    writer, tx = await _open_writer(dsn)
    conn = await asyncpg.connect(_plain(dsn))
    try:
        t0 = time.monotonic()
        await conn.execute(_init_sql_0046_block())
        assert time.monotonic() - t0 < 1, "객체가 있는데 잠금을 기다렸다"
        assert await conn.fetchval("SHOW lock_timeout") == "0", "블록 밖으로 lock_timeout 이 샜다"

        # 대조군 — 맨 CREATE UNIQUE INDEX IF NOT EXISTS 는 인덱스가 있어도 SHARE 를 기다린다.
        await conn.execute("SET lock_timeout = '1s'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
                "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
                "WHERE is_active = true AND scope = 'GLOBAL'"
            )
    finally:
        await conn.close()
        await tx.rollback()
        await writer.close()


async def test_init_sql_block_gives_up_after_lock_timeout_and_can_run_again(dsn):
    """0045 DB(0046 객체 없음)에서 열린 쓰기 뒤에 막히면 5초 뒤 깨끗이 실패한다 — 아무것도 만들지 않고,
    쓰기가 끝난 뒤 다시 돌리면 만든다(마이그레이션 Job 재실행과 같다)."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute("DROP TRIGGER rl_global_release_guard ON model.rate_limit_configs")
        await conn.execute("DROP INDEX model.uq_rate_limit_configs_global_active")
        writer, tx = await _open_writer(dsn)
        try:
            t0 = time.monotonic()
            with pytest.raises(asyncpg.LockNotAvailableError):
                await conn.execute(_init_sql_0046_block())
            waited = time.monotonic() - t0
        finally:
            await tx.commit()
            await writer.close()
        assert 4 < waited < 10, waited
        assert await conn.fetchval("SELECT to_regclass('model.uq_rate_limit_configs_global_active')") is None

        await conn.execute(_init_sql_0046_block())
        assert await conn.fetchval("SELECT to_regclass('model.uq_rate_limit_configs_global_active')")
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        ) == 1
    finally:
        await conn.close()


# ── 7. 이름을 그대로 쓰는 이유 ────────────────────────────────────────────────────────────


async def test_the_previous_init_sql_cannot_bring_the_single_slot_back(engine):
    """롤백한 차트의 Job 이 0046 DB 에서 이전 init SQL 을 다시 돌려도(그 Job 은 뒤이은 alembic 에서
    ``Can't locate revision`` 으로 실패하지만 init SQL 은 이미 커밋된 뒤다) 모델별 GLOBAL 은 그대로다."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    previous = _executed_0024_index_sql("uq_rate_limit_configs_active")  # 이전 init SQL 과 같은 정의
    await _global(engine, "claude-opus-4-7", rpm=60)
    async with engine.begin() as conn:
        await conn.execute(text(previous))  # 이름이 있으므로 no-op
    await _global(engine, "claude-sonnet-4-6", tpm=100000)
    assert len(await _all(engine, _ACTIVE_GLOBAL)) == 2

    # 대조군 — 그 이름이 없었다면(새 이름을 붙였다면) 같은 문장이 단일 슬롯을 되살린다.
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL rl.allow_global_release = 'on'"))  # 의도된 해제
        await conn.execute(text(
            "UPDATE model.rate_limit_configs SET is_active = false, updated_at = now() "
            "WHERE scope = 'GLOBAL' AND model_alias = 'claude-sonnet-4-6'"
        ))
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
        await conn.execute(text(previous))
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)
