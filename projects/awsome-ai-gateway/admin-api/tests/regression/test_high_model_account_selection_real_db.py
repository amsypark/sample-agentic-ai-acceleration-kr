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


# ── 모델 저장의 캐시 무효화는 커밋 뒤 ───────────────────────────────────────────────


async def test_model_saves_invalidate_the_cache_only_after_commit(api):
    """커밋 전에 지우면 그 창의 게이트웨이 캐시 미스가 저장 전 행(옛 계정)을 300초 다시 캐시한다."""
    client, _sql, cache = api
    await _register(client)
    r = await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))
    assert r.status_code == 201, r.text
    keys, in_tx = cache.calls[-1]
    assert keys == ["model:acctapi-claude", "model:anthropic.acctapi-claude", "model:list"]
    assert in_tx is False, "생성: 캐시를 커밋 전에 지웠다"

    r = await client.put("/admin/models/acctapi-claude", json={"account_name": None})
    assert r.status_code == 200, r.text
    keys, in_tx = cache.calls[-1]
    assert "model:acctapi-claude" in keys and "model:anthropic.acctapi-claude" in keys
    assert in_tx is False, "수정: 캐시를 커밋 전에 지웠다"


async def test_pricing_status_and_aws_sync_invalidate_the_cache_only_after_commit(api):
    """단가·상태 저장도 같다. 커밋 전에 지우면 그 창의 캐시 미스가 옛 단가를, 또는 비활성화 전의
    ACTIVE 를 300초 다시 캐시한다 — kill switch 가 그만큼 늦게 걸린다. 모델 id 키도 지워야 모델 id 로
    부른 요청이 옛 해석으로 서빙되지 않는다."""
    from datetime import datetime, timezone
    from decimal import Decimal
    from unittest.mock import patch

    from app.services.aws_price_list import AwsModelPrice, normalize_pmid

    client, _sql, cache = api
    assert (await client.post("/admin/models", json=_model_body())).status_code == 201
    want = ["model:acctapi-claude", "model:anthropic.acctapi-claude", "model:list"]

    def _last(label):
        keys, in_tx = cache.calls[-1]
        assert keys == want, (label, keys)
        assert in_tx is False, f"{label}: 캐시를 커밋 전에 지웠다"

    cache.calls.clear()
    r = await client.put("/admin/models/acctapi-claude/pricing", json={
        "input_price_per_1k_tokens": "0.004", "output_price_per_1k_tokens": "0.005",
        "effective_from": datetime.now(timezone.utc).isoformat(),
    })
    assert r.status_code == 200, r.text
    assert r.json()["current_pricing"]["input_price_per_1k_tokens"] == "0.004"
    _last("단가")

    r = await client.patch("/admin/models/acctapi-claude/status", json={"active": False})
    assert r.status_code == 200 and r.json()["status"] == "INACTIVE", r.text
    _last("비활성화")
    r = await client.patch("/admin/models/acctapi-claude/status", json={"active": True})
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE", r.text
    _last("활성화")

    pmid = normalize_pmid("anthropic.acctapi-claude")
    aws = {pmid: AwsModelPrice(provider_model_id=pmid, region_code="us-east-1", endpoint="runtime",
                               short={"input": Decimal("0.006"), "output": Decimal("0.007")}, long={})}
    calls_before = len(cache.calls)
    with patch("app.services.model_service.fetch_bedrock_prices", return_value=aws):
        r = await client.post("/admin/models/pricing/aws-sync",
                              json={"aliases": ["acctapi-claude"], "region_code": "us-east-1"})
    assert r.status_code == 200 and r.json()["synced"] == ["acctapi-claude"], r.text
    assert len(cache.calls) == calls_before + 1, "자동연동: alias 마다가 아니라 커밋 뒤 한 번 지운다"
    _last("자동연동")


# ── 같은 모델 id 의 형제 alias 와 계정 일치 ───────────────────────────────────────────

_SHARED = "acctapi-shared.model-id"  # 픽스처 정리(acctapi-%) 대상이어야 한다


async def _pair(client, account_name=None):
    for alias in ("acctapi-friendly", _SHARED):  # 시드처럼: 하나는 모델 id 와 이름이 같다
        r = await client.post("/admin/models", json=_model_body(alias=alias, pmid=_SHARED,
                                                                 account_name=account_name))
        assert r.status_code == 201, r.text


async def test_an_account_on_one_alias_of_a_shared_model_id_is_409_until_applied_to_all(api):
    client, sql, cache = api
    await _register(client)
    await _pair(client)

    r = await client.put("/admin/models/acctapi-friendly", json={"account_name": "acctapi-seoul"})
    assert r.status_code == 409, r.text
    assert f"{_SHARED} (no account)" in r.text and "apply_to_siblings" in r.text
    rows = await sql("SELECT alias, account_name FROM model.model_aliases "
                     "WHERE provider_model_id = :p ORDER BY alias", p=_SHARED)
    assert [tuple(x) for x in rows] == [("acctapi-friendly", None), (_SHARED, None)], "409 인데 썼다"

    cache.calls.clear()
    r = await client.put("/admin/models/acctapi-friendly",
                         json={"account_name": "acctapi-seoul", "apply_to_siblings": True})
    assert r.status_code == 200, r.text
    rows = await sql("SELECT alias, account_name, account_role_arn FROM model.model_aliases "
                     "WHERE provider_model_id = :p ORDER BY alias", p=_SHARED)
    assert [tuple(x) for x in rows] == [
        ("acctapi-friendly", "acctapi-seoul", ROLE), (_SHARED, "acctapi-seoul", ROLE)
    ], "형제의 사본(트리거)까지 같은 계정이어야 모델 id 로 부른 요청도 그 계정으로 간다"
    keys, in_tx = cache.calls[-1]
    assert keys == ["model:acctapi-friendly", f"model:{_SHARED}", "model:list"] and in_tx is False

    # 맞춘 뒤에는 관계없는 편집이 막히지 않는다.
    r = await client.put(f"/admin/models/{_SHARED}", json={"description": "x"})
    assert r.status_code == 200, r.text


async def test_creating_an_alias_that_disagrees_with_its_siblings_is_409(api):
    client, sql, _cache = api
    await _register(client)
    await _pair(client, account_name="acctapi-seoul")
    r = await client.post("/admin/models", json=_model_body(alias="acctapi-third", pmid=_SHARED))
    assert r.status_code == 409 and "acctapi-friendly (acctapi-seoul)" in r.text, r.text
    assert await sql("SELECT alias FROM model.model_aliases WHERE alias = 'acctapi-third'") == []
    r = await client.post("/admin/models", json=_model_body(alias="acctapi-third", pmid=_SHARED,
                                                             account_name="acctapi-seoul"))
    assert r.status_code == 201, r.text


async def test_activating_an_alias_that_disagrees_with_its_siblings_is_409(api):
    client, sql, _cache = api
    await _register(client)
    await _pair(client, account_name="acctapi-seoul")
    await sql("INSERT INTO model.model_aliases (alias, provider, provider_model_id, api_format, "
              "status, created_by) VALUES ('acctapi-dormant', 'BEDROCK', :p, 'BEDROCK_NATIVE', "
              "'INACTIVE', :u)", p=_SHARED, u=_SYS_USER)
    r = await client.patch("/admin/models/acctapi-dormant/status", json={"active": True})
    assert r.status_code == 409 and "then activate it" in r.text, r.text
    rows = await sql("SELECT status FROM model.model_aliases WHERE alias = 'acctapi-dormant'")
    assert rows[0][0] == "INACTIVE"


async def test_a_concurrent_create_cannot_slip_past_an_apply_to_all(api, dsn):
    """행 락은 아직 없는 행을 막지 못한다: 형제를 맞추는 저장이 커밋 전의 새 alias 를 못 보면 그
    alias 만 옛 계정으로 남는다. 모델 id 단위 advisory lock 이 두 저장을 직렬화한다."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.models import ModelCreateRequest, ModelUpdateRequest
    from app.services.model_service import ModelService

    client, sql, _cache = api
    await _register(client)
    await _register(client, name="acctapi-dedicated", role=ROLE2, ext=None)
    await _pair(client, account_name="acctapi-seoul")

    admin = CurrentUser(user_id=_SYS_USER, email="admin@example.invalid", role=UserRole.ADMIN,
                        team_id=None)
    svc = ModelService(cache_mgr=_RecordingCache())
    engine = create_async_engine(dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as creator, factory() as mover:
            await svc.create_model(creator, actor=admin, data=ModelCreateRequest(**_model_body(
                alias="acctapi-third", pmid=_SHARED, account_name="acctapi-seoul")))
            pid = (await mover.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            task = asyncio.ensure_future(svc.update_model(
                mover, alias="acctapi-friendly", actor=admin,
                data=ModelUpdateRequest(account_name="acctapi-dedicated", apply_to_siblings=True)))
            for _ in range(200):
                if task.done():
                    break
                waiting = await sql("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p", p=pid)
                if waiting and waiting[0][0] == "Lock":
                    break
                await asyncio.sleep(0.025)
            await creator.commit()
            await asyncio.wait_for(task, timeout=10)
            await mover.commit()
    finally:
        await engine.dispose()
    rows = await sql("SELECT alias, account_name FROM model.model_aliases "
                     "WHERE provider_model_id = :p ORDER BY alias", p=_SHARED)
    assert {a: n for a, n in rows} == {
        "acctapi-friendly": "acctapi-dedicated",
        "acctapi-third": "acctapi-dedicated",
        _SHARED: "acctapi-dedicated",
    }, "동시 생성된 alias 가 형제 맞추기를 빠져나가 옛 계정으로 남았다"


async def test_a_model_save_locks_the_registry_row_before_the_alias_row(api, dsn):
    """레지스트리 수정은 레지스트리 행 → 그 계정의 alias 행 순서로 잠근다. 모델 저장이 alias 행을 먼저
    잠그고 트리거에서 레지스트리 행을 기다리면 두 쓰기가 반대 순서라 교착(40P01)이 날 수 있다. 저장은
    레지스트리 행을 먼저 FOR SHARE 로 잡는다 — 그동안 alias 행은 잠겨 있지 않아야 한다."""
    import asyncio

    import asyncpg

    client, _sql, _cache = api
    await _register(client)
    r = await client.post("/admin/models", json=_model_body(account_name="acctapi-seoul"))
    assert r.status_code == 201, r.text

    plain = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    editor = await asyncpg.connect(plain)
    probe = await asyncpg.connect(plain)
    try:
        tx = editor.transaction()
        await tx.start()
        # 전파가 없는 수정(display_name)이라 editor 는 레지스트리 행만 잠근다.
        await editor.execute("UPDATE model.aws_accounts SET display_name = 'renamed' "
                             "WHERE name = 'acctapi-seoul'")
        task = asyncio.ensure_future(
            client.put("/admin/models/acctapi-claude", json={"description": "re-save"}))
        for _ in range(200):
            if task.done():
                break
            n = await probe.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                                     "current_database() AND wait_event_type = 'Lock'")
            if n:
                break
            await asyncio.sleep(0.025)
        assert not task.done(), "저장이 레지스트리 행 락을 기다리지 않았다"
        await probe.execute("SET lock_timeout = '200ms'")
        async with probe.transaction():
            await probe.execute("SELECT 1 FROM model.model_aliases WHERE alias = 'acctapi-claude' "
                                "FOR UPDATE NOWAIT")
        await tx.commit()
        r = await asyncio.wait_for(task, timeout=10)
        assert r.status_code == 200, r.text
    finally:
        await editor.close()
        await probe.close()
