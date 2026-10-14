# Copyright 2026 © Amazon.com and Affiliates.

"""계정 레지스트리 API + 모델의 계정 선택 — HTTP → 라우터 → 서비스 → 실 PG 트리거 → 응답.

mock 으로는 원리적으로 증명되지 않는 것만 본다:
  * 모델 저장 응답의 ``account_role_arn`` 이 **트리거가 DB 에서 채운 값**이다(refresh 가 빠지면
    응답은 저장 전 None 을 보여 준다);
  * 레지스트리 수정이 그 계정을 고른 alias 행을 실제로 옮기고, 라우터가 그 alias 들의 캐시 키를
    **커밋 뒤에** 지운다(커밋 전에 지우면 게이트웨이가 옛 행을 다시 캐시한다);
  * FK RESTRICT·UNIQUE·앱 프로필 ExternalId 충돌이 409 로, OPENMODEL·미등록 이름이 400 으로 나간다.

⚠️ PROOF_DSN 이 alembic head(0044 포함)를 마친 DB 를 가리켜야 한다. TEMPLATE 로 복제한 자기 소유
   DB(<name>_acctapiproof) 안에서만 쓴다.
"""

from __future__ import annotations

import os
import uuid

import pytest

# ⚠️ 모듈 전역이어야 한다. `from __future__ import annotations` 아래에서 FastAPI 는 의존성의
#    `request: Request` 애노테이션 문자열을 함수의 **모듈 전역**으로 푼다 — 픽스처 안에서
#    import 하면 풀지 못해 `request` 를 쿼리 파라미터로 보고 모든 요청이 422 가 된다(실측).
from fastapi import Request

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_acctapiproof"
_SYS_USER = uuid.UUID("00000000-0000-4000-a000-000000000010")  # db/init seed admin
ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
ROLE2 = "arn:aws:iam::444455556666:role/dedicated-bedrock"
APP_ROLE = "arn:aws:iam::777788889999:role/app-mantle"

pytestmark = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 트리거·FK·UNIQUE 는 실 PG 에서만 존재한다(0044 head)."
)


def _split(dsn):
    pre, _, name = dsn.rpartition("/")
    return pre, name


@pytest.fixture(scope="module")
async def dsn():
    pytest.importorskip("asyncpg")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                 "WHERE datname=:d AND pid<>pg_backend_pid()"), {"d": name})
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    yield f"{pre}/{owned}"
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


class _RecordingCache:
    """CacheInvalidationManager 대역 — 지운 키와 **그 순간 트랜잭션이 열려 있었는지**를 남긴다."""

    def __init__(self):
        self.calls: list[tuple[list[str], bool | None]] = []

    async def invalidate(self, keys, *, session=None):
        self.calls.append((list(keys), session.in_transaction() if session is not None else None))


@pytest.fixture
async def api(dsn):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.auth import CurrentUser, require_admin
    from app.core.db import SESSION_STATE_ATTR, get_db_session, install_commit_before_response
    from app.main import register_exception_handlers
    from app.models.auth import UserRole
    from app.routers import aws_accounts as accounts_router
    from app.routers import models as models_router
    from app.services.model_service import ModelService

    engine = create_async_engine(dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session(request: Request):
        async with factory() as s:
            setattr(request.state, SESSION_STATE_ATTR, s)
            try:
                yield s
            except Exception:
                await s.rollback()
                raise
            else:
                if s.in_transaction():
                    await s.commit()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(models_router.router)
    app.include_router(accounts_router.router)
    install_commit_before_response(app)
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[require_admin] = lambda: CurrentUser(
        user_id=_SYS_USER, email="admin@example.invalid", role=UserRole.ADMIN, team_id=None
    )
    cache = _RecordingCache()
    app.state.cache_mgr = cache
    app.state.model_service = ModelService(cache_mgr=cache)

    async def sql(stmt, **params):
        from sqlalchemy import text

        async with engine.begin() as c:
            res = await c.execute(text(stmt), params)
            return res.all() if res.returns_rows else []

    # 이 테스트가 만든 것만 지운다(시드는 그대로). alias → 계정 순(FK).
    await sql("DELETE FROM model.model_pricings WHERE model_alias LIKE 'acctapi-%'")
    await sql("DELETE FROM model.model_aliases WHERE alias LIKE 'acctapi-%'")
    await sql("DELETE FROM model.aws_accounts WHERE name LIKE 'acctapi-%'")
    await sql("UPDATE model.routing_profiles SET account_role_arn = NULL, external_id = NULL "
              "WHERE account_role_arn = :r", r=APP_ROLE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, sql, cache
    await engine.dispose()


def _model_body(alias="acctapi-claude", provider="BEDROCK", account_name=None, **kw):
    body = {
        "alias": alias,
        "provider": provider,
        "provider_model_id": kw.pop("pmid", "anthropic.acctapi-claude"),
        "api_format": "OPENAI_COMPATIBLE" if provider == "OPENMODEL" else "BEDROCK_NATIVE",
        "input_price_per_1k_tokens": "0.001",
        "output_price_per_1k_tokens": "0.002",
        **kw,
    }
    if account_name is not None:
        body["account_name"] = account_name
    return body


async def _register(client, name="acctapi-seoul", role=ROLE, ext="ext-1"):
    r = await client.post("/admin/aws-accounts", json={
        "name": name, "display_name": "In-Region Seoul", "role_arn": role, "external_id": ext,
    })
    assert r.status_code == 201, r.text
    return r.json()


async def test_selecting_an_account_answers_with_the_copy_the_gateway_signs_with(api):
    client, sql, _cache = api
    acct = await _register(client)
    assert acct["account_id"] == "111122223333" and acct["aliases"] == []

    r = await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["account_name"], body["account_role_arn"], body["account_external_id"]) == (
        "acctapi-seoul", ROLE, "ext-1"
    ), "응답이 트리거가 채운 사본이 아니다 — refresh 가 빠졌다"
    rows = await sql("SELECT account_name, account_role_arn, account_external_id "
                     "FROM model.model_aliases WHERE alias = 'acctapi-claude'")
    assert tuple(rows[0]) == ("acctapi-seoul", ROLE, "ext-1")

    listed = (await client.get("/admin/aws-accounts")).json()
    mine = next(a for a in listed["items"] if a["name"] == "acctapi-seoul")
    assert mine["aliases"] == ["acctapi-claude"]


async def test_openmodel_and_unknown_accounts_are_400_and_create_nothing(api):
    client, sql, _cache = api
    await _register(client)
    r = await client.post("/admin/models", json=_model_body(
        alias="acctapi-vllm", provider="OPENMODEL", account_name="acctapi-seoul"
    ))
    assert r.status_code == 400 and "OPENMODEL" in r.text
    r = await client.post("/admin/models", json=_model_body(alias="acctapi-x", account_name="acctapi-missing"))
    assert r.status_code == 400 and "acctapi-missing" in r.text
    assert await sql("SELECT alias FROM model.model_aliases WHERE alias IN ('acctapi-vllm', 'acctapi-x')") == []


async def test_omitting_keeps_and_an_explicit_null_clears_the_selection(api):
    client, sql, _cache = api
    await _register(client)
    assert (await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))).status_code == 201

    r = await client.put("/admin/models/acctapi-claude", json={"description": "kept"})
    assert r.status_code == 200 and r.json()["account_role_arn"] == ROLE
    r = await client.put("/admin/models/acctapi-claude", json={"account_name": None})
    assert r.status_code == 200, r.text
    assert (r.json()["account_name"], r.json()["account_role_arn"]) == (None, None)
    rows = await sql("SELECT account_name, account_role_arn FROM model.model_aliases "
                     "WHERE alias = 'acctapi-claude'")
    assert tuple(rows[0]) == (None, None)


async def test_a_registry_edit_repoints_the_aliases_and_invalidates_after_commit(api):
    client, sql, cache = api
    await _register(client)
    assert (await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))).status_code == 201
    cache.calls.clear()

    r = await client.put("/admin/aws-accounts/acctapi-seoul", json={"role_arn": ROLE2, "external_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["aliases"] == ["acctapi-claude"]
    rows = await sql("SELECT account_role_arn, account_external_id FROM model.model_aliases "
                     "WHERE alias = 'acctapi-claude'")
    assert tuple(rows[0]) == (ROLE2, None)
    keys, in_tx = cache.calls[-1]
    assert keys == ["model:acctapi-claude", "model:anthropic.acctapi-claude", "model:list"]
    assert in_tx is False, "캐시를 커밋 전에 지웠다 — 게이트웨이가 옛 계정을 다시 캐시할 수 있다"

    # 같은 값의 저장(콘솔은 폼 전체를 다시 보낸다)은 아무 alias 도 옮기지 않는다 — 지울 키도 없다.
    r = await client.put("/admin/aws-accounts/acctapi-seoul",
                         json={"role_arn": ROLE2, "external_id": None, "display_name": "renamed"})
    assert r.status_code == 200 and cache.calls[-1][0] == []


async def test_deleting_a_selected_account_is_409_until_it_is_released(api):
    client, _sql, _cache = api
    await _register(client)
    assert (await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))).status_code == 201
    r = await client.delete("/admin/aws-accounts/acctapi-seoul")
    assert r.status_code == 409 and "acctapi-claude" in r.text
    assert (await client.put("/admin/models/acctapi-claude", json={"account_name": None})).status_code == 200
    assert (await client.delete("/admin/aws-accounts/acctapi-seoul")).status_code == 204
    assert (await client.delete("/admin/aws-accounts/acctapi-seoul")).status_code == 404


async def test_one_role_is_one_entry_and_must_agree_with_an_app_profile(api):
    client, sql, _cache = api
    await _register(client)
    r = await client.post("/admin/aws-accounts", json={
        "name": "acctapi-dup", "display_name": "dup", "role_arn": ROLE,
    })
    assert r.status_code == 409 and "acctapi-seoul" in r.text

    # 앱 단위(라우팅 프로필) 계정과 같은 역할이면 ExternalId 도 같아야 한다.
    await sql("UPDATE model.routing_profiles SET account_role_arn = :r, external_id = 'app-ext' "
              "WHERE client = 'cowork'", r=APP_ROLE)
    r = await client.post("/admin/aws-accounts", json={
        "name": "acctapi-app", "display_name": "app", "role_arn": APP_ROLE, "external_id": "other",
    })
    assert r.status_code == 409 and "cowork" in r.text
    await _register(client, name="acctapi-app", role=APP_ROLE, ext="app-ext")
    listed = (await client.get("/admin/aws-accounts")).json()
    app_acct = next(a for a in listed["app_accounts"] if a["client"] == "cowork")
    assert (app_acct["account_id"], app_acct["registered_as"], app_acct["backend"]) == (
        "777788889999", "acctapi-app", "mantle"
    )


async def test_malformed_registry_input_is_a_422_naming_the_field(api):
    client, _sql, _cache = api
    r = await client.post("/admin/aws-accounts", json={
        "name": "default", "display_name": "x", "role_arn": ROLE,
    })
    assert r.status_code == 422 and "name" in r.text
    r = await client.post("/admin/aws-accounts", json={
        "name": "acctapi-user", "display_name": "x", "role_arn": "arn:aws:iam::111122223333:user/u",
    })
    assert r.status_code == 422 and "role_arn" in r.text
