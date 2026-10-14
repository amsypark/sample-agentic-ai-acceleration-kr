# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""백엔드↔콘솔 계약 수정분을 **실 PostgreSQL** 로 증명한다 (PROOF_DSN).

여기 모인 것은 전부 mock 으로는 증명이 원리적으로 불가능한 것들이다 — 검증 대상이
"WHERE 경계가 KST 로 정확히 잘리는가", "조인/GROUP BY 가 실제 스키마 위에서 도는가",
"NUMERIC 컬럼이 무엇을 거부하는가" 이기 때문이다. 단위 테스트의 SQL 문자열 단정은
경계 리터럴이 **있다**는 것만 보고, 그 경계로 **집계한 결과가 맞는지**는 보지 못한다.

  1. /admin/analytics 의 일자 범위(start_date/end_date) — 요약·모델·팀·사용자·추이가
     전부 같은 KST 범위로 잘리고, 범위 밖(월의 나머지)·실패 호출은 섞이지 않는다.

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
