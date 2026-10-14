# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""내 예산 카드의 '팀 예산 소진' 이 게이트웨이의 실제 판정과 같은지 — **실 Redis + 실 PostgreSQL** 로 증명한다.

게이트웨이는 팀 계층을 ``budget_check.lua`` 로 판정한다. 그 스크립트가 읽는 것은 gateway-proxy 가 요청마다 올리는
Redis 카운터 ``budget:team:{<team_id>}:<period>`` 와 admin-api 가 쓰는 config 캐시다. admin-api 의
``team.budget_exhausted`` (routers/my.py)는 같은 카운터를 같은 해석으로 읽어야 한다.

  * 워커 지연 — budget_usages(cost-recorder-worker 가 배치로 쌓는다)가 카운터보다 뒤처진 동안에도 카운터를 따른다.
  * 키 없음 = 0 — budget_check.lua 는 ``GET … or '0'`` 이고, 게이트웨이는 팀 카운터를 DB 에서 복원하지 않는다.
  * Redis 를 쓸 수 없으면 budget_usages 로(게이트웨이도 그때 DB 폴백으로 같은 행을 읽는다).

비교 대상은 손으로 옮긴 규칙이 아니라 **gateway-proxy 의 budget_check.lua 그 자체**다. config 캐시도 admin-api 의
실제 writer(``BudgetService._write_team_config_cache``)로 쓴다 — 두 배포물의 규약이 갈라지면 여기서 드러난다.

실행::

    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
        pytest tests/regression/test_high_my_budget_gateway_parity_real.py

PROOF_DSN 의 DB 는 바깥 트랜잭션 안에서만 쓰고 끝에 되돌린다(공유 DB 의 다른 증명 숫자를 움직이지 않는다).
Redis 에 쓴 키는 지운다.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
BUDGET_CHECK_LUA = (
    Path(__file__).resolve().parents[3] / "gateway-proxy" / "src" / "app" / "redis_scripts" / "budget_check.lua"
)
SEED_TEAM_ID = uuid.UUID("00000000-0000-4000-a000-000000000003")
SEED_ADMIN_ID = uuid.UUID("00000000-0000-4000-a000-000000000010")

# ⚠️ PROOF_DSN 이 있으면 증명 잡이다 — 그때 Redis 가 빠지면 skip 이 아니라 **실패**한다(조용히 사라지지 않게).
pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL + 실 Redis 가 필요하다")


@pytest.fixture
async def redis_client():
    if not REDIS_PROOF_URL:
        pytest.fail("PROOF_DSN 은 있는데 REDIS_PROOF_URL 이 없다 — 이 증명은 실 Redis 가 필요하다")
    redis_asyncio = pytest.importorskip("redis.asyncio", reason="redis 패키지 필요")
    client = redis_asyncio.from_url(REDIS_PROOF_URL, decode_responses=True)
    assert await client.ping() is True, f"REDIS_PROOF_URL 에 붙지 못했다: {REDIS_PROOF_URL}"
    client.created = []  # 정리용
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


def _period() -> str:
    from app.core.usage_filters import current_kst_period

    return current_kst_period()


async def _team(db, redis, limit: str, policy: str) -> SimpleNamespace:
    """팀 예산이 있는 팀 하나와 그 멤버 — DB 행은 트랜잭션 안에서만, config 캐시는 admin-api 의 실제 writer 로."""
    from sqlalchemy import text

    from app.models.budget import BudgetPolicy
    from app.services.budget_service import BudgetService

    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        text("INSERT INTO auth.teams (id, dept_id, name) SELECT :t, dept_id, 'Parity Team' FROM auth.teams WHERE id = :s"),
        {"t": team_id, "s": SEED_TEAM_ID},
    )
    await db.execute(
        text(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES (:u, :t, :e, 'parity', 'DEVELOPER', :sub)"
        ),
        {"u": user_id, "t": team_id, "e": f"parity-{user_id.hex[:12]}@example.com", "sub": f"sub-{user_id}"},
    )
    await db.execute(
        text(
            "INSERT INTO budget.budget_configs (scope, scope_id, max_budget_usd, policy, allocated_by, effective_from) "
            "VALUES ('TEAM', :t, :l, CAST(:p AS budget.budget_policy), :a, CURRENT_DATE)"
        ),
        {"t": team_id, "l": Decimal(limit), "p": policy, "a": SEED_ADMIN_ID},
    )
    cfg_key = f"budget:config:team:{{{team_id}}}"
    usage_key = f"budget:team:{{{team_id}}}:{_period()}"
    redis.created += [cfg_key, usage_key]
    writer = BudgetService(cache_mgr=SimpleNamespace(_redis=redis))
    await writer._write_team_config_cache(team_id, Decimal(limit), BudgetPolicy[policy], [80, 90, 100])
    return SimpleNamespace(team_id=team_id, user_id=user_id, cfg_key=cfg_key, usage_key=usage_key)


async def _db_usage(db, team_id: uuid.UUID, used: str) -> None:
    from sqlalchemy import text

    await db.execute(
        text(
            "INSERT INTO budget.budget_usages (scope, scope_id, period, client, used_usd, limit_usd) "
            "VALUES ('TEAM', :t, :p, NULL, :u, 0)"
        ),
        {"t": team_id, "p": _period(), "u": Decimal(used)},
    )


async def _gateway_denies(redis, team) -> bool:
    """gateway-proxy 가 이 팀의 다음 요청을 팀 계층에서 거부하는가 — 실제 budget_check.lua 로."""
    assert BUDGET_CHECK_LUA.exists(), f"gateway-proxy 스크립트를 찾지 못했다: {BUDGET_CHECK_LUA}"
    raw = await redis.eval(BUDGET_CHECK_LUA.read_text(encoding="utf-8"), 2, team.usage_key, team.cfg_key, "team")
    result = json.loads(raw)
    assert result["config_present"] is True, "config 캐시를 Lua 가 읽지 못했다 — 키 규약이 갈라졌다"
    return not result["allowed"]


async def _card_says_exhausted(db, team, redis) -> bool:
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.routers.my import get_my_budget

    request = MagicMock()
    request.app.state.redis = redis
    me = CurrentUser(user_id=team.user_id, email="parity@example.com", role=UserRole.DEVELOPER, team_id=None)
    return (await get_my_budget(request=request, user=me, session=db))["team"]["budget_exhausted"]


async def test_worker_lag_the_card_follows_the_gateway_counter(db, redis_client):
    """카운터는 100(게이트웨이가 요청마다 올린 값), budget_usages 는 50(워커가 아직 안 쌓음) — 게이트웨이는 거부한다."""
    team = await _team(db, redis_client, "100", "HARD_BLOCK")
    await _db_usage(db, team.team_id, "50")
    for cost in ("60", "40"):  # 게이트웨이처럼 INCRBYFLOAT 로 올린다
        await redis_client.incrbyfloat(team.usage_key, float(cost))

    assert await _gateway_denies(redis_client, team) is True, "전제: 게이트웨이가 이 팀을 거부한다"
    assert await _card_says_exhausted(db, team, redis_client) is True, "카드가 늦은 budget_usages 로 판정했다"


async def test_a_missing_counter_is_zero_for_both(db, redis_client):
    """카운터가 없으면 게이트웨이는 0 으로 읽고 통과시킨다 — budget_usages 가 소진이라고 해도 카드는 게이트웨이를 따른다."""
    team = await _team(db, redis_client, "100", "HARD_BLOCK")
    await _db_usage(db, team.team_id, "150")

    assert await _gateway_denies(redis_client, team) is False, "전제: 카운터가 없으면 게이트웨이는 통과시킨다"
    assert await _card_says_exhausted(db, team, redis_client) is False


async def test_redis_down_falls_back_to_budget_usages(db, redis_client):
    """Redis 에 붙지 못하면 budget_usages 로 판정한다 — 게이트웨이도 그때 DB 폴백으로 같은 행을 읽는다."""
    redis_asyncio = pytest.importorskip("redis.asyncio")
    team = await _team(db, redis_client, "100", "HARD_BLOCK")
    await _db_usage(db, team.team_id, "100")
    down = redis_asyncio.from_url("redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.5)
    try:
        assert await _card_says_exhausted(db, team, down) is True
    finally:
        await down.aclose()

    other = await _team(db, redis_client, "100", "HARD_BLOCK")  # 대조군 — DB 가 여유 있으면 소진이 아니다
    await _db_usage(db, other.team_id, "50")
    down = redis_asyncio.from_url("redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.5)
    try:
        assert await _card_says_exhausted(db, other, down) is False
    finally:
        await down.aclose()
