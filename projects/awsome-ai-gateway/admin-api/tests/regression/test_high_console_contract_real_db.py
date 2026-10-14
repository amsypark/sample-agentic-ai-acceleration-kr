# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""백엔드↔콘솔 계약 수정분을 **실 PostgreSQL** 로 증명한다 (PROOF_DSN).

여기 모인 것은 전부 mock 으로는 증명이 원리적으로 불가능한 것들이다 — 검증 대상이
"WHERE 경계가 KST 로 정확히 잘리는가", "조인/GROUP BY 가 실제 스키마 위에서 도는가",
"NUMERIC 컬럼이 무엇을 거부하는가" 이기 때문이다. 단위 테스트의 SQL 문자열 단정은
경계 리터럴이 **있다**는 것만 보고, 그 경계로 **집계한 결과가 맞는지**는 보지 못한다.

  1. /admin/analytics 의 일자 범위(start_date/end_date) — 요약·모델·팀·사용자·추이가
     전부 같은 KST 범위로 잘리고, 범위 밖(월의 나머지)·실패 호출은 섞이지 않는다.
  2. /admin/rate-limits 트리·실시간 사용량·오버라이드 제거 — 팀 미배정 사용자 조회
     (team_id IS NULL), 활성 alias 목록, 활성 GLOBAL 행, 상속 표식, ``measured`` 의 근거인
     "자기 스코프 활성 rpm 행", DELETE 후 재설정이 유니크 인덱스(0024)와 공존하는지, 그리고
     GLOBAL 이 **한 번에 한 모델**만 한도를 갖는 현재 불변식(콘솔 경고의 근거).
  3. ``SetBudgetRequest.max_budget_usd`` 의 상한(max_digits=12, decimal_places=4)이
     ``budget_configs.max_budget_usd NUMERIC(12,4)`` 와 **정확히 같은 경계**인지 — 스키마가
     통과시키는 최댓값은 컬럼이 받고, 스키마가 거부하는 최솟값은 컬럼도 거부한다(그 값이
     예전에는 스키마를 통과해 asyncpg overflow → 500 이 됐다).
  4. 채팅 assistant 메시지 INSERT 가 ``duration_ms`` 를 실제 스키마(0005)에 쓰고, 그 값이 done
     프레임의 ``durationMs`` 와 같다(⚠️ chat_agent 스키마는 migration 소유라 init SQL 로 만든
     owned DB 에는 없다 — 이 테스트만 alembic head 의 PROOF_DSN DB 를 쓰고 만든 행을 지운다).
  5. /admin/monitoring/models 의 status 가 등록부 LEFT JOIN 으로 ACTIVE / INACTIVE /
     UNREGISTERED 를 실제로 구분하고, 등록부에 없는 alias 의 트래픽이 사라지지 않는다.
  6. /admin/dashboard/top-teams 가 동명 팀을 서로 다른 ``team_id`` 의 두 행으로 낸다(콘솔이 이름으로
     조인하던 동안 두 팀의 소진율이 뒤바뀌었다 — 이 스키마에는 팀 이름 유일성 제약이 아예 없다).
  7. 자동 하향 PUT 의 ``enabled: false`` 가 budget.downgrade_policies 의 활성 행을 실제로 없앤다
     (예전: 규칙을 전부 is_active=true 로 다시 써서 '끄기' 저장 후에도 집행이 계속됐다).

실행::

    docker run -d --name pg-proof -p 55432:5432 -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=gwproof pgvector/pgvector:pg16
    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
        pytest tests/regression/test_high_console_contract_real_db.py

⚠️ ``PROOF_DSN`` 의 데이터베이스는 **건드리지 않는다.** 자기 소유 ``<dbname>_console``
   을 만들고 ``db/init/*.sql`` 을 적용해 쓰고 지운다(test_high_analytics_seed_reconciliation
   과 같은 규약 — 공용 DB 에 행을 섞으면 다른 증명 테스트의 숫자가 움직인다).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_console"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"

pytestmark = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요(mock/SQLite 로는 검증 불가)"
)

KST = timezone(timedelta(hours=9))

# init SQL(03_seed_data) 이 만드는 고정 행.
SEED_ADMIN_ID = uuid.UUID("00000000-0000-4000-a000-000000000010")
SEED_TEAM_ID = uuid.UUID("00000000-0000-4000-a000-000000000003")


# ─────────────────────────────────────────────────────────────────────────────
# 자체 소유 DB 부트스트랩
# ─────────────────────────────────────────────────────────────────────────────


def _split_dsn(dsn: str) -> tuple[str, str]:
    prefix, _, dbname = dsn.rpartition("/")
    assert prefix and dbname, f"DSN 형식이 이상하다: {dsn}"
    return prefix, dbname


def _asyncpg_dsn(sa_dsn: str) -> str:
    return sa_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _recreate_owned_db() -> str:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_SUFFIX}"
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


async def _drop_owned_db() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, dbname = _split_dsn(PROOF_DSN)
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{dbname}{_OWNED_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


async def _apply_init_sql(dsn: str) -> None:
    """실 ``db/init/*.sql`` 을 그대로 적용한다 — DDL 을 테스트에 복제하지 않는다.

    asyncpg 를 직접 쓰는 이유: init SQL 은 ``DO $$ … $$`` 와 멀티 스테이트먼트를 담고
    있어 SQLAlchemy 의 prepared statement 경로가 거부한다.
    """
    asyncpg = pytest.importorskip("asyncpg")
    files = sorted(_INIT_DIR.glob("*.sql"))
    assert len(files) >= 4, f"init SQL 을 {len(files)}개만 찾았다 — 경로 확인: {_INIT_DIR}"
    conn = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        for f in files:
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001 - 어떤 파일이 왜 실패했는지 보여야 한다
                print(f"  [init] {f.name} 실패(계속): {type(e).__name__}: {e}")
        # 대조군 — 이 파일이 요구하는 테이블이 실제로 생겼는가(없이 통과하면 공허하다).
        for schema, table in [
            ("auth", "users"),
            ("auth", "teams"),
            ("usage", "usage_logs"),
            ("budget", "budget_usages"),
            ("model", "model_aliases"),
            ("model", "rate_limit_configs"),
        ]:
            n = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = $1 AND table_name = $2",
                schema,
                table,
            )
            assert n == 1, f"{schema}.{table} 이 없다 — init SQL 적용 실패"
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 시드 — 1. analytics 일자 범위
# ─────────────────────────────────────────────────────────────────────────────

PERIOD = "2026-09"
TEAM_B_ID = uuid.UUID("bbbb0000-0000-4000-a000-0000000000b0")
U_A = uuid.UUID("aaaa0000-0000-4000-a000-0000000000a1")  # team = seed team
U_B = uuid.UUID("aaaa0000-0000-4000-a000-0000000000b1")  # team = TEAM_B
U_SEED = uuid.UUID("aaaa0000-0000-4000-a000-0000000000c1")  # seed team, 월 말 사용 + 이관분
ALIAS = "claude-sonnet-4-6"


def _kst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


async def _seed_analytics(conn) -> None:
    dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
    assert dept_id is not None, "시드 팀이 없다 — init SQL 의 03_seed_data 확인"
    await conn.execute(
        "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1, $2, 'Range Team B')",
        TEAM_B_ID,
        dept_id,
    )
    for uid, team, email in [
        (U_A, SEED_TEAM_ID, "range-a@example.com"),
        (U_B, TEAM_B_ID, "range-b@example.com"),
        (U_SEED, SEED_TEAM_ID, "range-seed@example.com"),
    ]:
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1, $2, $3, $4, 'DEVELOPER', $5)",
            uid,
            team,
            email,
            email.split("@")[0],
            f"sub-{uid}",
        )
    provider = await conn.fetchval(
        "SELECT provider::text FROM model.model_aliases WHERE alias = $1", ALIAS
    )
    assert provider, f"시드 alias {ALIAS} 가 없다"

    async def log(user, team, at: datetime, cost: str, status: str = "SUCCESS"):
        await conn.execute(
            "INSERT INTO usage.usage_logs "
            "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
            " requested_at, completed_at, cost_usd, input_tokens, output_tokens, "
            " cache_read_tokens, cache_creation_tokens, latency_ms) "
            "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, $5, "
            "        $6::usage.usage_status, $7, $7, $8, 10, 20, 0, 0, 100)",
            user, team, dept_id, ALIAS, provider, status, at, Decimal(cost),
        )

    # 경계가 핵심이다 — 범위 [09-01, 09-05] 는 KST 09-01 00:00 ~ 09-06 00:00(반개구간).
    await log(U_A, SEED_TEAM_ID, _kst(9, 1, 0, 30), "1.00")  # = UTC 08-31 15:30 → 범위 안
    await log(U_A, SEED_TEAM_ID, _kst(9, 5, 23, 59), "2.00")  # 범위 마지막 순간 → 안
    await log(U_A, SEED_TEAM_ID, _kst(9, 6, 0, 0), "4.00")  # 다음 날 0시 → 밖(월 안)
    await log(U_B, TEAM_B_ID, _kst(9, 3, 12), "8.00")  # 범위 안, 다른 팀
    await log(U_B, TEAM_B_ID, _kst(9, 3, 13), "16.00", status="ERROR")  # 실패 → 어디에도 없음
    await log(U_B, TEAM_B_ID, _kst(8, 31, 23, 59), "32.00")  # 전월 마지막 순간 → 9월 밖
    await log(U_SEED, SEED_TEAM_ID, _kst(9, 20, 12), "64.00")  # 월 안, 범위 밖
    # 이관분(seed) = 164 − 64(월 전-status 실사용) = 100
    await conn.execute(
        "INSERT INTO budget.budget_usages "
        "(id, scope, scope_id, period, client, used_usd, limit_usd, last_updated) "
        "VALUES (gen_random_uuid(), 'USER'::budget.budget_scope, $1, $2, NULL, 164.00, 1000, now())",
        U_SEED,
        PERIOD,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 시드 — 2. rate-limit 트리
# ─────────────────────────────────────────────────────────────────────────────

U_TEAMLESS = uuid.UUID("aaaa0000-0000-4000-a000-0000000000d1")
RETIRED_ALIAS = "llama-3-70b"  # 비활성으로 내리고 GLOBAL 한도를 남긴다
BRACKET_ALIAS = "claude-sonnet-4-6[1m]"


async def _seed_rate_limits(conn) -> None:
    await conn.execute(
        "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
        "VALUES ($1, NULL, 'rl-teamless@example.com', 'rl-teamless', 'DEVELOPER', $2)",
        U_TEAMLESS,
        f"sub-{U_TEAMLESS}",
    )

    async def rl(scope, scope_id, *, alias=None, rpm=None, tpm=None):
        await conn.execute(
            "INSERT INTO model.rate_limit_configs "
            "(id, scope, scope_id, model_alias, rpm_limit, tpm_limit, is_active, created_by) "
            "VALUES (gen_random_uuid(), $1::model.rate_limit_scope, $2, $3, $4, $5, true, $6)",
            scope, scope_id, alias, rpm, tpm, SEED_ADMIN_ID,
        )

    await rl("TEAM", SEED_TEAM_ID, rpm=100, tpm=9000)
    await rl("USER", U_A, rpm=60)
    await conn.execute(
        "UPDATE model.model_aliases SET status = 'INACTIVE' WHERE alias = $1", RETIRED_ALIAS
    )
    await rl("GLOBAL", None, alias=RETIRED_ALIAS, tpm=5000)  # TPM 만 — rpm 카운터 없음


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def owned_dsn():
    asyncpg = pytest.importorskip("asyncpg")
    dsn = await _recreate_owned_db()
    await _apply_init_sql(dsn)
    conn = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        await _seed_analytics(conn)
        await _seed_rate_limits(conn)
    finally:
        await conn.close()
    yield dsn
    await _drop_owned_db()


@pytest.fixture
async def session(owned_dsn):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(owned_dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _admin():
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    return CurrentUser(user_id=uuid.uuid4(), email="admin@example.com", role=UserRole.ADMIN, team_id=None)


async def _analytics(session, *, group_by="model", start=None, end=None, period=PERIOD):
    from app.services.analytics_service import AnalyticsService

    return await AnalyticsService().get_analytics(
        session,
        period=period,
        group_by=group_by,
        scope="all",
        start_date=start,
        end_date=end,
        actor=_admin(),
    )


def _ours(items, attr="email"):
    """시드 데이터에서 온 행만 — init SQL 이 다른 사용 로그를 넣지는 않지만, 넣더라도 섞이지 않게."""
    return {getattr(i, attr): i for i in items if (getattr(i, attr) or "").startswith("range-")}


# ─────────────────────────────────────────────────────────────────────────────
# 1. analytics 일자 범위
# ─────────────────────────────────────────────────────────────────────────────


async def test_month_request_is_the_control(session):
    """대조군 — 범위 없이 월 전체: SUCCESS 만, 9월 KST 만(전월 32·실패 16 제외)."""
    resp = await _analytics(session)
    assert resp.start_date is None and resp.end_date is None
    assert resp.cost_summary.total_cost_usd == Decimal("79.00")  # 1+2+4+8+64
    assert resp.cost_summary.total_requests == 5


async def test_range_narrows_every_summary_aggregate(session):
    """[09-01, 09-05] = 1 + 2 + 8 = 11. 범위가 무시되면 위 대조군의 79 가 나온다."""
    resp = await _analytics(session, start="2026-09-01", end="2026-09-05")
    assert (resp.start_date, resp.end_date) == ("2026-09-01", "2026-09-05")
    s = resp.cost_summary
    assert s.total_cost_usd == Decimal("11.00"), "범위가 요약 카드에 반영되지 않았다"
    assert s.total_requests == 3
    assert s.active_users == 2
    assert s.total_tokens == 3 * 30
    assert {m.model: (m.cost_usd, m.requests) for m in resp.by_model} == {
        ALIAS: (Decimal("11.00"), 3)
    }


async def test_range_boundaries_are_kst_half_open(session):
    """KST 자정 경계 양쪽 — 09-01 00:30 KST(=UTC 전날 15:30)는 안, 09-06 00:00 KST 는 밖."""
    first_day = await _analytics(session, start="2026-09-01", end="2026-09-01")
    assert first_day.cost_summary.total_cost_usd == Decimal("1.00")
    after = await _analytics(session, start="2026-09-06", end="2026-09-06")
    assert after.cost_summary.total_cost_usd == Decimal("4.00")


async def test_cross_month_range_reaches_into_the_previous_month(session):
    """[08-31, 09-01] — 전월 마지막 순간(32)과 9월 첫 건(1). 월 필터로는 만들 수 없는 질의다."""
    resp = await _analytics(session, start="2026-08-31", end="2026-09-01")
    assert resp.cost_summary.total_cost_usd == Decimal("33.00")


async def test_range_narrows_team_breakdown_and_trends(session):
    resp = await _analytics(session, group_by="team", start="2026-09-01", end="2026-09-05")
    by_team = {t.team_id: t.cost_usd for t in resp.by_team}
    assert by_team == {str(SEED_TEAM_ID): Decimal("3.00"), str(TEAM_B_ID): Decimal("8.00")}
    assert [(t.date, t.cost_usd) for t in resp.trends] == [
        ("2026-09-01", Decimal("1.00")),
        ("2026-09-03", Decimal("8.00")),
        ("2026-09-05", Decimal("2.00")),
    ], "추이 차트가 범위 밖 날짜(09-06, 09-20)를 그린다"


async def test_month_to_date_range_keeps_the_seed(session):
    """월 초 시작 범위는 `/usage/by-user` 와 같은 규약으로 월 seed 를 더한다(U_SEED: 0 + 100)."""
    resp = await _analytics(session, group_by="user", start="2026-09-01", end="2026-09-05")
    ours = _ours(resp.by_user)
    assert ours["range-a@example.com"].cost_usd == Decimal("3.00")
    assert ours["range-b@example.com"].cost_usd == Decimal("8.00")
    assert ours["range-seed@example.com"].cost_usd == Decimal("100.00")


async def test_mid_month_range_does_not_attribute_the_seed(session):
    """음성 대조 — 09-02 시작 범위에 월 seed 를 더하면 이관 이전 금액이 그 며칠에 귀속된다."""
    resp = await _analytics(session, group_by="user", start="2026-09-02", end="2026-09-05")
    ours = _ours(resp.by_user)
    assert "range-seed@example.com" not in ours, "중간 범위에 seed 가 섞였다"
    assert ours["range-a@example.com"].cost_usd == Decimal("2.00")


async def test_month_mode_by_user_is_unchanged(session):
    """대조군 — 월 질의의 seed 반영(64 + 100)은 그대로다."""
    resp = await _analytics(session, group_by="user")
    assert _ours(resp.by_user)["range-seed@example.com"].cost_usd == Decimal("164.00")


async def test_team_export_csv_follows_the_range_and_axis(session):
    import csv
    import io

    from app.services.analytics_service import AnalyticsService

    content, ctype = await AnalyticsService().export_analytics(
        session,
        format="csv",
        period=PERIOD,
        group_by="team",
        start_date="2026-09-01",
        end_date="2026-09-05",
        actor=_admin(),
    )
    assert ctype == "text/csv"
    rows = list(csv.reader(io.StringIO(content)))
    assert rows[0] == ["period", "team", "team_id", "cost_usd", "active_users"]
    got = {r[2]: (r[0], Decimal(r[3])) for r in rows[1:]}
    assert got[str(TEAM_B_ID)] == ("2026-09-01~2026-09-05", Decimal("8"))
    assert got[str(SEED_TEAM_ID)] == ("2026-09-01~2026-09-05", Decimal("3"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. rate-limit 트리 / 실시간 사용량 / 오버라이드 제거
# ─────────────────────────────────────────────────────────────────────────────


def _rl_service():
    """Redis 는 대역(빈 SCAN) — 여기서 증명할 것은 DB 쪽이다. 실 Redis SCAN 은
    test_high_rate_limit_live_usage_real_redis.py 가 따로 증명한다."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.rate_limit_service import RateLimitService

    async def _empty(*_a, **_kw):
        if False:  # pragma: no cover
            yield None

    redis = AsyncMock()
    redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _empty())
    return RateLimitService(cache_mgr=CacheInvalidationManager(redis))


async def _tree(session):
    return await _rl_service().get_rate_limit_tree(session)


def _find(nodes, node_id: str):
    for n in nodes:
        if n.id == node_id:
            return n
        for c in n.children:
            if c.id == node_id:
                return c
    return None


async def _active_rows(session, scope: str, scope_id=None):
    from sqlalchemy import text

    sql = (
        "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
        "WHERE scope = CAST(:scope AS model.rate_limit_scope) AND is_active "
        + ("AND scope_id = :sid" if scope_id is not None else "AND scope_id IS NULL")
    )
    params = {"scope": scope, **({"sid": scope_id} if scope_id is not None else {})}
    return (await session.execute(text(sql), params)).all()


async def test_tree_lists_every_active_alias_as_a_global_root(session):
    from sqlalchemy import text

    active = {
        r[0]
        for r in (
            await session.execute(text("SELECT alias FROM model.model_aliases WHERE status = 'ACTIVE'"))
        ).all()
    }
    assert BRACKET_ALIAS in active, "시드 전제가 깨졌다"
    nodes = await _tree(session)
    global_ids = {n.id for n in nodes if n.scope == "GLOBAL"}
    assert active <= global_ids, f"트리에 없는 활성 alias: {sorted(active - global_ids)}"
    assert _find(nodes, BRACKET_ALIAS).config is None


async def test_inactive_alias_with_a_live_global_cap_is_still_listed(session):
    nodes = await _tree(session)
    retired = _find(nodes, RETIRED_ALIAS)
    assert retired is not None and retired.scope == "GLOBAL"
    assert retired.config is not None
    assert (retired.config.rpm, retired.config.tpm, retired.config.target_id) == (
        None,
        5000,
        RETIRED_ALIAS,
    )


async def test_teamless_user_is_a_root(session):
    nodes = await _tree(session)
    roots = {n.id: n for n in nodes if n.scope == "USER"}
    assert str(U_TEAMLESS) in roots, "team_id IS NULL 사용자가 트리에서 빠졌다"
    assert roots[str(U_TEAMLESS)].inherited_from is None
    # 팀 멤버는 루트로 중복 등장하지 않는다.
    assert str(U_A) not in roots


async def test_inherited_member_carries_the_team_marker(session):
    nodes = await _tree(session)
    seed_member = _find(nodes, str(U_SEED))  # 시드 팀 소속, 자기 행 없음
    assert seed_member.inherited_from == str(SEED_TEAM_ID)
    assert seed_member.config.scope == "TEAM" and seed_member.config.rpm == 100
    own = _find(nodes, str(U_A))
    assert own.inherited_from is None and own.config.scope == "USER" and own.config.rpm == 60


async def test_measured_reads_only_the_subjects_own_rpm_row(session):
    svc = _rl_service()
    own = await svc.get_live_usage("USER", str(U_A), session=session)
    assert (own["measured"], own["limit_rpm"]) == (True, 60)
    # 상속 중인 사용자에게는 USER 카운터가 생기지 않는다 — 팀 한도를 끌어오면 거짓 '측정'.
    inheriting = await svc.get_live_usage("USER", str(U_SEED), session=session)
    assert (inheriting["measured"], inheriting["limit_rpm"]) == (False, None)
    team = await svc.get_live_usage("TEAM", str(SEED_TEAM_ID), session=session)
    assert (team["measured"], team["limit_rpm"]) == (True, 100)
    # TPM 만 걸린 GLOBAL 은 rpm 카운터가 없다 → 측정 불가가 정답이다.
    glob = await svc.get_live_usage("GLOBAL", RETIRED_ALIAS, session=session)
    assert (glob["measured"], glob["limit_rpm"]) == (False, None)


async def test_delete_override_restores_inheritance_and_reset_still_works(session):
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.rate_limits import RateLimitSetRequest

    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    svc = _rl_service()

    out = await svc.delete_user_rate_limit(session, user_id=U_A, actor=actor)
    await session.commit()
    assert out.deactivated == 1
    assert await _active_rows(session, "USER", U_A) == []
    node = _find(await _tree(session), str(U_A))
    assert node.inherited_from == str(SEED_TEAM_ID), "제거 후 팀 상속으로 돌아오지 않았다"

    again = await svc.delete_user_rate_limit(session, user_id=U_A, actor=actor)
    await session.commit()
    assert again.deactivated == 0, "멱등이 아니다"

    # 제거 후 재설정 — 0024 의 부분 유니크 인덱스와 충돌하지 않아야 한다.
    await svc.set_user_rate_limit(session, user_id=U_A, data=RateLimitSetRequest(rpm=30), actor=actor)
    await session.commit()
    assert [(r.rpm_limit,) for r in await _active_rows(session, "USER", U_A)] == [(30,)]


async def test_global_limit_is_single_slot_today(session):
    """현재 불변식을 **고정**한다: 다른 모델의 GLOBAL 을 저장하면 앞 모델의 한도가 해제된다.

    upsert 가 alias 와 무관하게 GLOBAL 활성 행을 전부 비활성화하고, 0024 의 유니크 인덱스도
    ``(scope, COALESCE(scope_id, nil))`` 라 GLOBAL 활성 행은 최대 1개다. 콘솔이 GLOBAL 노드를
    보여주게 되면서 이 동작이 콘솔에서 도달 가능해졌다 — admin-ui 는 저장 전에 경고한다.
    이 테스트가 깨지면(=모델별 GLOBAL 이 공존하게 되면) 그 경고 문구를 지워야 한다.
    """
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.rate_limits import RateLimitSetRequest

    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    before = await _active_rows(session, "GLOBAL")
    assert [r.model_alias for r in before] == [RETIRED_ALIAS], "시드 전제가 깨졌다"

    await _rl_service().set_global_rate_limit(
        session, model_alias="claude-opus-4-7", data=RateLimitSetRequest(rpm=10), actor=actor
    )
    await session.commit()

    after = await _active_rows(session, "GLOBAL")
    assert [r.model_alias for r in after] == ["claude-opus-4-7"]
    nodes = await _tree(session)
    assert _find(nodes, RETIRED_ALIAS) is None, "비활성 모델은 한도가 해제되면 트리에서 내려간다"
    assert _find(nodes, "claude-opus-4-7").config.rpm == 10


# ─────────────────────────────────────────────────────────────────────────────
# 3. 예산 금액 상한 = 컬럼 폭
# ─────────────────────────────────────────────────────────────────────────────


async def _insert_budget(owned_dsn, amount: str):
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO budget.budget_configs "
                "(id, scope, scope_id, max_budget_usd, policy, allocated_by, effective_from, is_active) "
                # is_active=false — 활성 행 유니크 인덱스(0024)와 다른 증명에 영향을 주지 않는다.
                "VALUES (gen_random_uuid(), 'USER'::budget.budget_scope, gen_random_uuid(), "
                "        $1, 'HARD_BLOCK'::budget.budget_policy, $2, CURRENT_DATE, false)",
                Decimal(amount),
                SEED_ADMIN_ID,
            )
    finally:
        await conn.close()


@pytest.mark.parametrize("amount", ["99999999.9999", "0", "0.0001"])
async def test_schema_accepted_amounts_fit_the_column(owned_dsn, amount):
    from app.schemas.budgets import SetBudgetRequest

    SetBudgetRequest(max_budget_usd=Decimal(amount))  # 스키마 통과
    await _insert_budget(owned_dsn, amount)  # 컬럼도 받는다


@pytest.mark.parametrize("amount", ["100000000", "100000000.0000", "123456789.5"])
async def test_schema_rejected_amounts_also_overflow_the_column(owned_dsn, amount):
    """스키마가 거부하는 값은 컬럼에서도 overflow 다 — 즉 422 가 막는 것은 진짜 500 이다."""
    asyncpg = pytest.importorskip("asyncpg")
    from pydantic import ValidationError

    from app.schemas.budgets import SetBudgetRequest

    with pytest.raises(ValidationError):
        SetBudgetRequest(max_budget_usd=Decimal(amount))
    with pytest.raises(asyncpg.exceptions.NumericValueOutOfRangeError):
        await _insert_budget(owned_dsn, amount)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 채팅 done 프레임의 durationMs ↔ chat_agent.messages.duration_ms
# ─────────────────────────────────────────────────────────────────────────────


class _Body:
    """botocore StreamingBody 대역 — AgentCore 가 흘리는 `data: <json>` 라인."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def iter_lines(self, chunk_size: int = 1024):  # noqa: ARG002 - 시그니처 호환
        yield from self._lines


async def test_assistant_message_persists_the_done_frame_duration(monkeypatch):
    import json as _json

    import boto3
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers import chat_agent as m

    engine = create_async_engine(PROOF_DSN)
    sid = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            if await conn.scalar(text("SELECT to_regclass('chat_agent.messages')")) is None:
                pytest.skip("chat_agent 스키마가 없다 — PROOF_DSN DB 에 alembic upgrade head 를 먼저")
            await conn.execute(
                text("INSERT INTO chat_agent.sessions (id, user_id) VALUES (:id, :uid)"),
                {"id": sid, "uid": SEED_ADMIN_ID},
            )

        lines = [
            f"data: {_json.dumps({'type': 'text', 'chunk': '답'})}".encode(),
            f"data: {_json.dumps({'type': 'done', 'session_id': 's', 'phase': 4})}".encode(),
        ]
        client = type("C", (), {"invoke_agent_runtime": lambda self, **_k: {"response": _Body(lines)}})()
        monkeypatch.setattr(boto3, "client", lambda *_a, **_k: client)
        monkeypatch.setattr(m, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))

        relay = m._StreamRelay()
        await m._agentcore_producer(relay, str(sid), "질문")

        done = [f.decode() for f in relay.frames if f.decode().startswith("event: done")]
        assert len(done) == 1, relay.frames
        frame_ms = _json.loads(done[0].split("data:", 1)[1].strip())["durationMs"]

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT content, duration_ms FROM chat_agent.messages "
                        "WHERE session_id = :sid AND role = 'assistant'"
                    ),
                    {"sid": sid},
                )
            ).one()
        assert row.content == "답"
        assert row.duration_ms == frame_ms, "라이브 footer 와 복원 footer 가 다른 숫자를 보인다"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM chat_agent.sessions WHERE id = :id"), {"id": sid})
        await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 5. 모니터링 모델 상태 — 등록부 LEFT JOIN
# ─────────────────────────────────────────────────────────────────────────────


async def test_monitoring_status_reflects_the_registry(owned_dsn):
    """최근 1시간 트래픽: 활성 alias / 비활성 alias(시드에서 INACTIVE 로 내림) / 등록부에 없는 alias."""
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers.monitoring import UNREGISTERED_MODEL_STATUS, monitoring_models

    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
        for alias in ("claude-opus-4-8", RETIRED_ALIAS, "ghost-model-proof", "ghost-model-proof"):
            await conn.execute(
                "INSERT INTO usage.usage_logs "
                "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
                " requested_at, completed_at, cost_usd, input_tokens, output_tokens, latency_ms) "
                "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, 'BEDROCK', "
                "        'SUCCESS'::usage.usage_status, now() - interval '5 minutes', now(), 0.01, 1, 1, 50)",
                U_A, SEED_TEAM_ID, dept_id, alias,
            )
    finally:
        await conn.close()

    engine = create_async_engine(owned_dsn)
    try:
        async with async_sessionmaker(engine)() as session:
            out = await monitoring_models(request=MagicMock(), admin=MagicMock(), session=session)
    finally:
        await engine.dispose()

    by_alias = {m["alias"]: m for m in out["models"]}
    assert by_alias["claude-opus-4-8"]["status"] == "ACTIVE"
    assert by_alias[RETIRED_ALIAS]["status"] == "INACTIVE", "비활성 모델이 ACTIVE 로 보인다"
    ghost = by_alias.get("ghost-model-proof")
    assert ghost is not None, "등록부에 없는 alias 의 트래픽이 조인에서 사라졌다"
    assert ghost["status"] == UNREGISTERED_MODEL_STATUS
    assert ghost["last_1h_requests"] == 2, "GROUP BY 가 status 때문에 행을 쪼개거나 합쳤다"


# ─────────────────────────────────────────────────────────────────────────────
# 6. 대시보드 top-teams — 동명 팀
# ─────────────────────────────────────────────────────────────────────────────


async def test_top_teams_keeps_same_named_teams_apart(owned_dsn):
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers.dashboard import top_teams

    twin_1, twin_2 = uuid.uuid4(), uuid.uuid4()
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
        for tid in (twin_1, twin_2):
            await conn.execute(
                "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1, $2, 'Twin Team')", tid, dept_id
            )
        # 다른 증명(2026-09)과 섞이지 않게 2026-07 에 쌓는다.
        for tid, cost in ((twin_1, "500.00"), (twin_2, "400.00")):
            await conn.execute(
                "INSERT INTO usage.usage_logs "
                "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
                " requested_at, completed_at, cost_usd, input_tokens, output_tokens, latency_ms) "
                "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, 'BEDROCK', "
                "        'SUCCESS'::usage.usage_status, $5, $5, $6, 1, 1, 50)",
                U_A, tid, dept_id, ALIAS, _kst(7, 10, 12), Decimal(cost),
            )
    finally:
        await conn.close()

    engine = create_async_engine(owned_dsn)
    try:
        async with async_sessionmaker(engine)() as session:
            out = await top_teams(period="2026-07", limit=5, client=None, _admin=MagicMock(), session=session)
    finally:
        await engine.dispose()

    twins = [t for t in out["teams"] if t["name"] == "Twin Team"]
    assert [(t["team_id"], t["cost_usd"]) for t in twins] == [(str(twin_1), 500.0), (str(twin_2), 400.0)], (
        "동명 팀이 하나로 합쳐졌거나 team_id 가 없다 — 콘솔은 이 id 로 소진율을 조인한다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. 자동 하향 끄기
# ─────────────────────────────────────────────────────────────────────────────


async def test_downgrade_enabled_false_really_turns_it_off(session):
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.models.budget import BudgetScope
    from app.schemas.budgets import AutoDowngradeConfigRequest
    from app.services.budget_service import BudgetService

    cache = MagicMock()
    cache.invalidate = AsyncMock()
    svc = BudgetService(cache_mgr=cache)
    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    rules = [{"from_model_alias": "claude-opus-4-8", "to_model_alias": "claude-sonnet-4-6", "threshold_pct": 80}]

    async def active_rules() -> int:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM budget.downgrade_policies "
                    "WHERE scope = 'TEAM' AND scope_id = :sid AND is_active"
                ),
                {"sid": SEED_TEAM_ID},
            )
        ).scalar_one()

    # 대조군 — 켜서 저장하면 활성 규칙이 생긴다(시드 팀에는 03_seed_data 의 팀 예산이 있다).
    on = await svc.set_downgrade_config(
        session,
        scope=BudgetScope.TEAM,
        scope_id=SEED_TEAM_ID,
        data=AutoDowngradeConfigRequest(enabled=True, rules=rules),
        actor=actor,
    )
    await session.commit()
    assert on.enabled is True and await active_rules() == 1

    # 콘솔은 체크를 끈 상태에서도 규칙과 함께 PUT 한다(AutoDowngradeConfig.handleSave).
    off = await svc.set_downgrade_config(
        session,
        scope=BudgetScope.TEAM,
        scope_id=SEED_TEAM_ID,
        data=AutoDowngradeConfigRequest(enabled=False, rules=rules),
        actor=actor,
    )
    await session.commit()
    assert off.enabled is False
    assert await active_rules() == 0, "끄기 저장 후에도 활성 규칙이 남아 gateway-proxy 가 계속 바꿔치기한다"
