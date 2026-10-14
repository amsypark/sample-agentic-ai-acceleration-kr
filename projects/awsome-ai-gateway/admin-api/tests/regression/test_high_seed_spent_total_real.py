# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``PUT /admin/budgets/spent`` (seed_spent) — 앱별 값을 시드한 뒤 사용자 **총액** 카운터가 두 번 세지지 않는지.
**실 PostgreSQL + 실 Redis** 로 증명한다.

seed_spent 는 앱별(client) 항목을 시드하면 총액 키 ``budget:user:{<id>}:<period>`` 도 다시 쓴다. 그런데
budget_usages 에는 총합 행(client NULL)과 앱별 행이 **함께** 산다 — cost-recorder-worker 가 둘 다 쓰고, 총합 행은
앱별 지출을 이미 포함한다. 예전 코드는 "그 사용자 행 전부의 합" 으로 다시 써서, 총액 → 앱 순서로 시드하면 앱
지출이 두 번 들어갔다(100 + 30 = 130). 게이트웨이는 그 키로 개인 예산을 판정하므로 실제보다 일찍 막는다. 게이트웨이가
키를 잃었을 때 DB 에서 복원하는 값도 총합 행 하나다(gateway-proxy budget_service.check_budget).

실행::

    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
        pytest tests/regression/test_high_seed_spent_total_real.py

PROOF_DSN 의 DB 는 바깥 트랜잭션 안에서만 쓰고 끝에 되돌린다. Redis 에 쓴 키는 지운다.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
SEED_TEAM_ID = uuid.UUID("00000000-0000-4000-a000-000000000003")
SEED_ADMIN_ID = uuid.UUID("00000000-0000-4000-a000-000000000010")
PERIOD = "2026-09"

# ⚠️ PROOF_DSN 이 있으면 증명 잡이다 — 그때 Redis 가 빠지면 skip 이 아니라 **실패**한다.
pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL + 실 Redis 가 필요하다")


@pytest.fixture
async def redis_client():
    if not REDIS_PROOF_URL:
        pytest.fail("PROOF_DSN 은 있는데 REDIS_PROOF_URL 이 없다 — 이 증명은 실 Redis 가 필요하다")
    redis_asyncio = pytest.importorskip("redis.asyncio", reason="redis 패키지 필요")
    client = redis_asyncio.from_url(REDIS_PROOF_URL, decode_responses=True)
    assert await client.ping() is True, f"REDIS_PROOF_URL 에 붙지 못했다: {REDIS_PROOF_URL}"
    client.created = []
    yield client
    if client.created:
        await client.delete(*client.created)
    await client.aclose()


@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await trans.rollback()
    finally:
        await engine.dispose()


async def _user(db, redis) -> uuid.UUID:
    from sqlalchemy import text

    uid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES (:u, :t, :e, 'seed', 'DEVELOPER', :sub)"
        ),
        {"u": uid, "t": SEED_TEAM_ID, "e": f"seed-{uid.hex[:12]}@example.com", "sub": f"sub-{uid}"},
    )
    redis.created += [f"budget:user:{{{uid}}}:{PERIOD}"] + [
        f"budget:user:{{{uid}}}:{c}:{PERIOD}" for c in ("claude-code", "cowork", "codex")
    ]
    return uid


async def _seed(db, redis, uid: uuid.UUID, client: str | None, spent: str) -> None:
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.budgets import SeedSpentItem
    from app.services.budget_service import BudgetService

    admin = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    svc = BudgetService(cache_mgr=SimpleNamespace(_redis=redis))
    item = SeedSpentItem(scope="USER", scope_id=str(uid), client=client, period=PERIOD, spent_usd=Decimal(spent))
    out = await svc.seed_spent(db, items=[item], actor=admin)
    assert out.failed == 0, out.results


async def _total_row(db, uid: uuid.UUID) -> Decimal | None:
    from sqlalchemy import text

    return (
        await db.execute(
            text(
                "SELECT used_usd FROM budget.budget_usages "
                "WHERE scope = 'USER' AND scope_id = :u AND period = :p AND client IS NULL"
            ),
            {"u": uid, "p": PERIOD},
        )
    ).scalar_one_or_none()


async def test_seeding_an_app_after_the_total_does_not_count_it_twice(db, redis_client):
    uid = await _user(db, redis_client)
    await _seed(db, redis_client, uid, None, "100")  # 총액 100 — 앱 지출을 포함한 값이다
    await _seed(db, redis_client, uid, "claude-code", "30")  # 그 안의 claude-code 30

    total_key = await redis_client.get(f"budget:user:{{{uid}}}:{PERIOD}")
    assert Decimal(total_key) == Decimal("100"), f"총액 카운터에 앱 지출이 두 번 들어갔다: {total_key}"
    # 게이트웨이가 키를 잃었을 때 DB 에서 복원하는 값(총합 행)과 같다.
    assert await _total_row(db, uid) == Decimal("100")
    assert Decimal(await redis_client.get(f"budget:user:{{{uid}}}:claude-code:{PERIOD}")) == Decimal("30")


async def test_seeding_only_apps_totals_the_apps(db, redis_client):
    """대조군 — 총합 행이 없으면 시드한 앱 지출의 합이 총액이다."""
    uid = await _user(db, redis_client)
    await _seed(db, redis_client, uid, "claude-code", "30")
    await _seed(db, redis_client, uid, "cowork", "20")

    assert Decimal(await redis_client.get(f"budget:user:{{{uid}}}:{PERIOD}")) == Decimal("50")
    assert await _total_row(db, uid) is None
