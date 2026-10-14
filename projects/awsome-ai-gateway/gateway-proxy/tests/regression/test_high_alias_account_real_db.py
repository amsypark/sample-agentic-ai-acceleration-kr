# Copyright 2026 © Amazon.com and Affiliates.

"""alias 계정(0043/0044)이 **실 Postgres 값에서** 게이트웨이의 서명 대상까지 흐르는지.

왜 실 DB 인가
-------------
allowed_clients 게이트는 gateway-proxy ORM 이 컬럼을 선언하지 않아 무력화된 채 배포된 적이 있다
(test_critical_model_client_scope_real_db.py). alias 계정은 같은 부류에서 더 나쁘다: 값이 빠지면
거부가 아니라 **다른 계정에 청구되는 200** 이 된다. 그리고 이 값은 admin-api 가 쓰는 것이 아니라
DB 트리거가 레지스트리 선택에서 복사한 사본이다 — 트리거·ORM 컬럼·_orm_to_schema 매핑·resolver
중 어느 하나가 끊겨도 단위 테스트는 통과한다. 그래서 여기서는 레지스트리에 계정을 넣고 alias 에서
**이름으로** 고른 뒤, 실제 RouterService 로 읽어 AccountTarget 까지 본다.

같은 provider_model_id 를 공유하는 alias 들이 서로 다른 계정을 부르면 모델 id 로 지목한 요청은
거절되어야 하고(``_pmid_fallback_row``), 계정이 같으면 이름순 첫 alias 로 결정적으로 해석돼야 한다.

⚠️ 트랜잭션을 롤백한다(DDL 없음). PROOF_DSN 은 db/init + alembic upgrade head(0044 포함) 를 마친
   DB 를 가리켜야 한다.
"""

from __future__ import annotations

import os
import uuid

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")

pytestmark = pytest.mark.skipif(
    not PROOF_DSN,
    reason="PROOF_DSN 미설정 — 트리거가 채운 사본이 리졸버까지 흐르는지는 실 PG 로만 증명된다.",
)

_PREFIX = "zzproof-acct-"
ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
ROLE2 = "arn:aws:iam::444455556666:role/dedicated-bedrock"


async def _seed(session, actor_id: str) -> None:
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO auth.users (id, email, display_name, role, sso_subject) "
            "VALUES (:id, :email, 'acct-proof', 'ADMIN', :sub)"
        ),
        {"id": actor_id, "email": f"{actor_id}@example.invalid", "sub": actor_id},
    )
    await session.execute(
        text(
            "INSERT INTO model.aws_accounts "
            "(name, display_name, role_arn, external_id, created_by) "
            "VALUES ('zzproof-acct', 'proof', :role, 'ext-proof', :actor)"
        ),
        {"role": ROLE, "actor": actor_id},
    )
    rows = [
        # (alias, provider_model_id, account_name)
        ("a", "anthropic.acct-proof-x", "zzproof-acct"),  # 계정 있음
        ("b", "anthropic.acct-proof-x", None),  # 같은 모델 id, 계정 없음 → 불일치
        ("c", "anthropic.acct-proof-y", "zzproof-acct"),  # 같은 모델 id, 같은 계정 → 일치
        ("d", "anthropic.acct-proof-y", "zzproof-acct"),
    ]
    for alias, pmid, account in rows:
        await session.execute(
            text(
                "INSERT INTO model.model_aliases "
                "(alias, provider, provider_model_id, api_format, status, created_by, "
                "account_name) "
                "VALUES (:alias, 'BEDROCK', :pmid, 'ANTHROPIC_MESSAGES', 'ACTIVE', :actor, :acct)"
            ),
            {"alias": _PREFIX + alias, "pmid": pmid, "actor": actor_id, "acct": account},
        )
    await session.flush()


@pytest.fixture
async def session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(PROOF_DSN)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            try:
                await _seed(s, str(uuid.uuid4()))
                yield s
            finally:
                await s.rollback()  # 공용 proof DB 를 더럽히지 않는다
    finally:
        await engine.dispose()


async def test_a_registry_selection_reaches_the_signing_target(session):
    """DB(레지스트리 + 이름 선택) → 트리거 사본 → ORM → 스키마 → AccountTarget."""
    from app.services.account_credentials import (
        AccountTarget,
        effective_account_id,
        resolve_account_target,
    )
    from app.services.router_service import RouterService

    cfg = await RouterService().resolve_bedrock_model(None, session, _PREFIX + "a")
    assert (cfg.account_role_arn, cfg.account_external_id) == (ROLE, "ext-proof")
    assert resolve_account_target(cfg) == AccountTarget(ROLE, "ext-proof", source="alias")
    assert effective_account_id(cfg) == "111122223333"


async def test_an_alias_without_a_selection_resolves_to_no_account(session):
    """계정 없는 alias = 오늘 동작(프로필 → 파드) — resolver 는 None 이어야 한다."""
    from app.services.account_credentials import resolve_account_target
    from app.services.router_service import RouterService

    cfg = await RouterService().resolve_bedrock_model(None, session, _PREFIX + "b")
    assert cfg.account_role_arn is None and resolve_account_target(cfg) is None


async def test_a_registry_edit_reaches_the_gateway_read_path(session):
    from sqlalchemy import text

    from app.services.router_service import RouterService

    await session.execute(
        text(
            "UPDATE model.aws_accounts SET role_arn = :r, external_id = NULL "
            "WHERE name = 'zzproof-acct'"
        ),
        {"r": ROLE2},
    )
    await session.flush()
    cfg = await RouterService().resolve_bedrock_model(None, session, _PREFIX + "a")
    assert (cfg.account_role_arn, cfg.account_external_id) == (ROLE2, None)


async def test_a_model_id_whose_aliases_call_different_accounts_is_refused(session):
    from app.services.router_service import RouterService

    with pytest.raises(LookupError, match="different AWS accounts"):
        await RouterService().resolve_bedrock_model(None, session, "anthropic.acct-proof-x")


async def test_a_model_id_whose_aliases_agree_resolves_to_the_first_by_name(session):
    from app.services.router_service import RouterService

    cfg = await RouterService().resolve_bedrock_model(None, session, "anthropic.acct-proof-y")
    assert cfg.alias == _PREFIX + "c" and cfg.account_role_arn == ROLE


async def test_the_seeded_model_id_siblings_resolve_deterministically(session):
    """시드 claude-sonnet-4-6 / claude-sonnet-4-6[1m] 은 같은 모델 id 다. 순서 없는 limit(1) 은
    DB 에 따라 [1m] 을 골랐다(로컬 compose DB 에서 실측). 이름순이면 언제나 짧은 alias 다."""
    from app.services.router_service import RouterService

    cfg = await RouterService().resolve_bedrock_model(
        None, session, "global.anthropic.claude-sonnet-4-6"
    )
    assert cfg.alias == "claude-sonnet-4-6"


async def test_nothing_was_left_behind():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text("SELECT count(*) FROM model.model_aliases WHERE alias LIKE :p"),
                    {"p": f"{_PREFIX}%"},
                )
            ).scalar_one()
            m = (
                await conn.execute(
                    text("SELECT count(*) FROM model.aws_accounts WHERE name = 'zzproof-acct'")
                )
            ).scalar_one()
        assert (n, m) == (0, 0), "롤백이 되지 않았다"
    finally:
        await engine.dispose()
