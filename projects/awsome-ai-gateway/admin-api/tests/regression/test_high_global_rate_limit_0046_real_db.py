# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""0046 — GLOBAL rate limit 을 모델별로. 마이그레이션 함수 자체를 실 PostgreSQL 에서 고정한다.

  1. ``db/init`` 이 만드는 rate-limit 인덱스와 0046 ``upgrade()`` 가 만드는 인덱스가 ``pg_get_indexdef``
     로 글자까지 같다. init SQL 은 매 마이그레이션 Job 에서 alembic 보다 먼저 돌고 compose·CI 는 init SQL
     로 스키마를 만든다 — 둘이 갈리면 신규 설치와 업그레이드가 서로 다른 제약으로 돈다.
  2. 여러 모델이 실제 GLOBAL 한도(rpm 또는 tpm)를 가지면 ``downgrade()`` 는 그 목록과 정리 SQL 을 담아
     실패하고 아무것도 바꾸지 않는다. 판정이 없으면 같은 데이터에서 0045 인덱스 생성이 PostgreSQL 의
     중복 키 오류로 죽는다(대조군) — 어느 모델의 한도를 지워야 하는지 말해 주지 않는다.
  3. 비운 행(rpm·tpm 모두 NULL)은 아무것도 집행하지 않으므로 ``downgrade()`` 가 비활성화하고 0045 의 단일
     슬롯을 되살린다 — 두 번째 모델의 GLOBAL 행이 다시 거부된다(대조군). 다시 ``upgrade()`` 하면 받아들여진다.
  4. 0046 이 USER/TEAM 인덱스의 **이름을 그대로** 쓰는 이유: 이전 차트의 init SQL(옛 정의의
     ``CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_active``)이 0046 DB 에서 다시 돌아도 no-op 이다.
     대조군: 그 이름이 없으면 같은 문장이 단일 슬롯을 조용히 되살린다.

마이그레이션 함수는 alembic 없이 돈다(이 서비스의 lock 에 alembic 이 없다). 모듈의 ``from alembic import op``
자리에 ``execute`` / ``get_bind`` 만 가진 대역을 넣고, alembic 의 ``transaction_per_migration`` 처럼 한
트랜잭션에서 실행한다 — 예외면 롤백된다. 문자열 SQL 은 alembic 처럼 ``text()`` 로 감싼다. 실 alembic CLI
왕복(0045 → 0046 → 0045 → 0046)은 이 파일이 대신하지 않는다.

⚠️ ``PROOF_DSN`` 의 DB 는 건드리지 않는다. 자기 소유 ``<dbname>_rl0046`` 을 init SQL 로 만들어 쓰고 지운다.
"""

from __future__ import annotations

import importlib.util
import os
import sys
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
_M0046 = Path(__file__).resolve().parents[3] / "db" / "versions" / "0046_global_rate_limit_per_model.py"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

_UNIQUE_INDEXES = (
    "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
    "WHERE indrelid = 'model.rate_limit_configs'::regclass AND indisunique AND NOT indisprimary "
    "ORDER BY 1"
)
_ACTIVE_GLOBAL = (
    "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
    "WHERE scope = 'GLOBAL' AND is_active ORDER BY model_alias"
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


async def _run(engine, module, func: str) -> None:
    """alembic 의 transaction_per_migration 처럼 함수 하나를 트랜잭션 하나로 돌린다(예외면 롤백)."""

    def _call(sync_conn) -> None:
        module._test_op.bind = sync_conn
        getattr(module, func)()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _admin_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _ = _split_dsn(PROOF_DSN)
    return create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")


@pytest.fixture
async def engine():
    """테스트마다 init SQL 만으로 새 DB — 마이그레이션 함수가 인덱스를 바꾸므로 공유하지 않는다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

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
    eng = create_async_engine(f"{prefix}/{owned}")
    yield eng
    await eng.dispose()
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _all(engine, sql: str) -> list[tuple]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def _global(engine, alias: str, *, rpm=None, tpm=None) -> None:
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


async def test_init_sql_and_the_migration_build_the_same_indexes(engine, m0046):
    init_only = await _all(engine, _UNIQUE_INDEXES)
    assert [name for name, _ in init_only] == [
        "model.uq_rate_limit_configs_active",
        "model.uq_rate_limit_configs_global_active",
    ]

    await _run(engine, m0046, "downgrade")  # GLOBAL 행이 없다 → 통과
    single_slot = await _all(engine, _UNIQUE_INDEXES)
    assert [name for name, _ in single_slot] == ["model.uq_rate_limit_configs_active"]
    assert single_slot != init_only[:1], "대조군 — 비교가 정의 차이를 실제로 잡는다"

    await _run(engine, m0046, "upgrade")
    assert await _all(engine, _UNIQUE_INDEXES) == init_only


async def test_downgrade_refuses_while_two_models_hold_a_limit(engine, m0046):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6", tpm=200000)
    await _global(engine, "claude-sonnet-4-6[1m]")  # 비운 행 — 혼자서는 막지 않는다
    before = (await _all(engine, _UNIQUE_INDEXES), await _all(engine, _ACTIVE_GLOBAL))

    with pytest.raises(RuntimeError) as refused:
        await _run(engine, m0046, "downgrade")
    message = str(refused.value)
    assert "claude-opus-4-7: rpm=60, tpm=None" in message
    assert "claude-sonnet-4-6: rpm=None, tpm=200000" in message
    assert "claude-sonnet-4-6[1m]" not in message, "비운 행은 한도가 아니다"
    assert "IS DISTINCT FROM '<alias to keep>'" in message and "Nothing was changed" in message
    # 트랜잭션이 롤백됐다 — 비운 행의 비활성화까지 되돌아갔다.
    assert (await _all(engine, _UNIQUE_INDEXES), await _all(engine, _ACTIVE_GLOBAL)) == before

    # 대조군 — 판정이 없으면 같은 데이터에서 0045 인덱스가 PostgreSQL 의 중복 키 오류로 죽는다.
    with pytest.raises(IntegrityError, match="could not create unique index"):
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
            await conn.execute(text(m0046.CREATE_SINGLE_SLOT_INDEX))


async def test_downgrade_drops_empty_rows_and_restores_the_single_slot(engine, m0046, capsys):
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6")  # 비운 행 — 아무것도 집행하지 않는다

    await _run(engine, m0046, "downgrade")
    assert "deactivated 1 active GLOBAL row(s) with neither rpm nor tpm" in capsys.readouterr().out
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]

    # 대조군 — 0045 의 단일 슬롯이 돌아왔다: 두 번째 모델의 GLOBAL 행이 거부된다.
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)

    await _run(engine, m0046, "upgrade")
    await _global(engine, "claude-sonnet-4-6", tpm=100000)
    assert await _all(engine, _ACTIVE_GLOBAL) == [
        ("claude-opus-4-7", 60, None),
        ("claude-sonnet-4-6", None, 100000),
    ]


async def test_the_previous_init_sql_cannot_bring_the_single_slot_back(engine):
    """롤백한 차트의 Job 이 0046 DB 에서 이전 init SQL 을 다시 돌려도(그 Job 은 뒤이은 alembic 에서
    ``Can't locate revision '0046'`` 으로 실패하지만 init SQL 은 이미 커밋된 뒤다) 모델별 GLOBAL 은 그대로다."""
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
        await conn.execute(text(
            "UPDATE model.rate_limit_configs SET is_active = false "
            "WHERE scope = 'GLOBAL' AND model_alias = 'claude-sonnet-4-6'"
        ))
        await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
        await conn.execute(text(previous))
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)
