# Copyright 2026 © Amazon.com and Affiliates.

"""alias 계정 → 라우팅 프로필 계정(오늘 규칙) → 파드 — **실제 요청 처리기**를 통과시켜 본다.

왜 처리기 단위인가
------------------
라이브 배포가 앱 단위 계정에 의존한다: claude-code 의 프로필은 Bedrock native 를 다른 계정으로
보내고(AssumeRole 이 실패하면 in-account 로 투명 폴백), cowork 는 Mantle 을 앱 계정으로, codex 는
in-account 다. alias 계정이 없는 alias 는 이 동작이 **바이트 단위로 그대로**여야 한다. 그것은
resolver 하나를 단위 테스트해서는 증명되지 않는다 — 판정이 라우터(어느 어댑터를 만들까)와
어댑터(어느 클라이언트로 부를까)에 나뉘어 있기 때문이다. 그래서 여기서는 ``messages()``,
``count_tokens()``, ``/model/*`` 패스스루, ``/v1/responses``, ``/v1/chat/completions`` 를 **진짜
어댑터·진짜 자격증명 provider** 로 돌리고, 가짜는 맨 끝(STS 와 boto3/HTTP)에만 둔다. 어느
자격증명이 실제로 서명했는지는 가짜 boto3 클라이언트가 응답 본문에 적어 돌려준다.

시나리오의 프로필은 라이브 세 앱의 **모양**만 따른다(값은 문서용 자리표시자).
"""

from __future__ import annotations

import datetime as dt
import io
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError
from starlette.datastructures import Headers

from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.mantle_adapter import MantleAdapter
from app.providers.registry import ProviderRegistry
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.schemas.routing import RoutingProfileSchema
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_STATUS,
    AccountTarget,
)
from app.services.bedrock_account_client import BedrockAccountClientProvider
from app.services.router_service import RouterService

pytestmark = pytest.mark.asyncio

PROFILE_ROLE = "arn:aws:iam::444455556666:role/claude-code-bedrock"
ALIAS_ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
OTHER_ROLE = "arn:aws:iam::777788889999:role/fallback-account"
POD_REGION = "us-east-2"
PROFILE_REGION = "ap-northeast-2"
RUNTIME_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"


def _cfg(alias, *, role=None, ext=None, provider=ProviderType.BEDROCK, pmid=None, endpoint=""):
    return ModelConfigSchema(
        provider_model_id=pmid or f"global.anthropic.{alias}",
        alias=alias,
        provider=provider,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint=endpoint,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002")),
        status=ModelStatus.ACTIVE,
        account_role_arn=role,
        account_external_id=ext,
    )


def _claude_code_profile(**kw):
    """claude-code 모양: backend=invoke + 다른 계정 역할 + ExternalId + 그 계정 리전."""
    return RoutingProfileSchema(
        client="claude-code",
        backend="invoke",
        account_role_arn=PROFILE_ROLE,
        region=PROFILE_REGION,
        default_model=None,
        external_id="claude-code-ext",
        **kw,
    )


def _cowork_profile(default_model=None):
    """cowork 모양: backend=mantle + Mantle 용 다른 계정 역할."""
    return RoutingProfileSchema(
        client="cowork",
        backend="mantle",
        account_role_arn=PROFILE_ROLE,
        region="ap-northeast-1",
        default_model=default_model,
        external_id="cowork-ext",
    )


def _codex_profile(backend="mantle"):
    """codex 모양: in-account(역할 없음)."""
    return RoutingProfileSchema(
        client="codex",
        backend=backend,
        account_role_arn=None,
        region=POD_REGION,
        default_model="gpt-5.6-terra",
        external_id=None,
    )


def _auth():
    return AuthContext(
        user_id="00000000-0000-0000-0000-000000000001",
        team_id="00000000-0000-0000-0000-000000000002",
        dept_id="00000000-0000-0000-0000-000000000003",
        roles=[Role.DEVELOPER],
        auth_type=AuthType.VIRTUAL_KEY,
        key_id="k1",
    )


# ── 끝단 가짜: STS 와 boto3 ─────────────────────────────────────────────────────────


class _STS:
    """역할마다 다른 키를 준다 — 어느 AssumeRole 의 자격증명인지 응답에서 구별하려고."""

    def __init__(self, fail_roles=()):
        self.calls: list[dict] = []
        self._fail = set(fail_roles)

    def assume_role(self, **kw):
        self.calls.append(kw)
        if kw["RoleArn"] in self._fail:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")
        acct = kw["RoleArn"].split(":")[4]
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA-{acct}",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
            }
        }


def _fake_boto(signer: str, region: str, *, fail_code=None, built=None):
    """invoke_model / count_tokens 가 **누가 서명했는지**를 응답에 적는 bedrock-runtime 대역."""
    c = MagicMock()
    c.meta.region_name = region

    def _invoke(**kw):
        if fail_code:
            raise ClientError({"Error": {"Code": fail_code, "Message": "x"}}, "InvokeModel")
        body = {
            "signed_by": signer,
            "region": region,
            "model": kw["modelId"],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        return {
            "body": io.BytesIO(json.dumps(body).encode()),
            "ResponseMetadata": {"RequestId": f"rid-{signer}"},
        }

    c.invoke_model = MagicMock(side_effect=_invoke)
    c.count_tokens = MagicMock(side_effect=lambda **kw: {"inputTokens": len(signer)})
    return c


@pytest.fixture
def world(monkeypatch):
    """진짜 어댑터 + 진짜 provider. 가짜는 STS·boto3 뿐."""
    sts = _STS()
    built: list[dict] = []

    def _boto_client(svc, **kw):
        built.append({"svc": svc, **kw})
        return _fake_boto(kw["aws_access_key_id"], kw["region_name"])

    monkeypatch.setattr("app.services.bedrock_account_client.boto3.client", _boto_client)
    provider = BedrockAccountClientProvider(sts_client=sts, boto_config="CFG", now=lambda: 1000.0)
    pod = _fake_boto("POD", POD_REGION)
    registry = ProviderRegistry()
    registry.register(
        ProviderType.BEDROCK,
        BedrockAdapter(pod, account_clients=provider, account_region=POD_REGION),
    )
    w = SimpleNamespace(sts=sts, built=built, provider=provider, pod=pod, registry=registry)
    return w


class _Router(RouterService):
    """DB 만 대역화한다(게이트는 상속받은 진짜 구현)."""

    def __init__(self, configs: dict[str, ModelConfigSchema]):
        self._configs = configs

    def _get(self, ref):
        if ref not in self._configs:
            raise LookupError(f"Model alias '{ref}' not found")
        return self._configs[ref]

    async def alias_provider(self, redis, db, alias):
        cfg = self._configs.get(alias)
        return cfg.provider if cfg else None

    async def resolve_bedrock_model(self, redis, db, ref):
        return self._get(ref)

    async def resolve_mantle_model(
        self, redis, db, ref, expected_provider=ProviderType.BEDROCK_MANTLE
    ):
        return self._get(ref)

    async def resolve_codex_model(self, redis, db, ref):
        return self._get(ref)

    async def resolve_openai_model(self, redis, db, ref):
        return self._get(ref)


class _CB:
    def __init__(self):
        self.failures: list[str] = []

    async def is_open(self, redis, pmid):
        return False

    async def try_acquire_halfopen_probe(self, redis, pmid):
        return True

    async def record_failure(self, redis, pmid):
        self.failures.append(pmid)

    async def record_success(self, redis, pmid):
        return None


class _FakeRequest:
    def __init__(self, body: dict, state: dict, app_state):
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = SimpleNamespace(state=app_state)
        self.headers = Headers()
        self.url = SimpleNamespace(path="/v1/messages")

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


def _app_state(world, profile, *, fallback_order=None, alias_map=None, cb=None, extra=None):
    loader = MagicMock()
    loader.load = AsyncMock(return_value=profile)
    st = SimpleNamespace(
        provider_registry=world.registry,
        cost_recorder=MagicMock(finalize=AsyncMock(return_value=Decimal("0"))),
        routing_profile_loader=loader,
        bedrock_account_client_provider=world.provider,
        agentcore_mcp_client=None,
        body_logger=None,
        circuit_breaker=cb or _CB(),
        fallback_resolver=(
            SimpleNamespace(resolve=lambda **kw: list(fallback_order)) if fallback_order else None
        ),
        alias_provider_map=alias_map or {},
        tokenizer=None,
        metrics=None,
    )
    for k, v in (extra or {}).items():
        setattr(st, k, v)
    return st


async def _messages(monkeypatch, world, profile, configs, alias, *, client="claude-code", **app_kw):
    import app.routers.messages as messages_mod

    monkeypatch.setattr(messages_mod, "_router_service", _Router(configs))
    app_state = _app_state(world, profile, **app_kw)
    state = {
        "auth_context": _auth(),
        "client": client,
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-1",
    }
    req = _FakeRequest(
        {"model": alias, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        state,
        app_state,
    )
    resp = await messages_mod.messages(req)
    return resp, app_state


def _body(resp):
    return json.loads(bytes(resp.body))


def _finalize_kwargs(app_state):
    return app_state.cost_recorder.finalize.await_args.kwargs


# ── Bedrock native: claude-code 형 프로필 ───────────────────────────────────────────


async def test_claude_code_alias_without_account_goes_to_the_profile_account_as_today(
    monkeypatch, world
):
    resp, st = await _messages(
        monkeypatch, world, _claude_code_profile(), {"claude-x": _cfg("claude-x")}, "claude-x"
    )
    assert resp.status_code == 200
    assert _body(resp)["signed_by"] == "ASIA-444455556666"
    assert _body(resp)["region"] == PROFILE_REGION
    assert world.sts.calls == [
        {
            "RoleArn": PROFILE_ROLE,
            "RoleSessionName": "gw-bedrock-xacct",
            "DurationSeconds": 3600,
            "ExternalId": "claude-code-ext",
        }
    ]
    # 모델 id 는 프로필 리전으로 재작성된다(오늘 동작): global. 은 그대로.
    assert _body(resp)["model"] == "global.anthropic.claude-x"
    assert _finalize_kwargs(st)["profile_account"] == AccountTarget(
        PROFILE_ROLE, "claude-code-ext", source="profile"
    )


async def test_claude_code_assume_failure_still_falls_back_in_account_as_today(monkeypatch, world):
    """라이브 안전장치: 프로필 계정 AssumeRole 이 AccessDenied 여도 claude-code 는 200.

    in-account 로 투명 폴백한다 — 이 기능 이전과 같다.
    """
    world.sts._fail.add(PROFILE_ROLE)
    resp, st = await _messages(
        monkeypatch, world, _claude_code_profile(), {"claude-x": _cfg("claude-x")}, "claude-x"
    )
    assert resp.status_code == 200 and _body(resp)["signed_by"] == "POD"
    # 기록은 실제로 서명한 쪽(파드)이다 — 프로필 계정으로 기록하면 정산이 "미매칭" 이 된다.
    assert _finalize_kwargs(st)["profile_account"] is None


async def test_claude_code_alias_with_an_account_uses_it_in_the_profile_region(monkeypatch, world):
    resp, st = await _messages(
        monkeypatch,
        world,
        _claude_code_profile(),
        {"claude-x": _cfg("claude-x", role=ALIAS_ROLE, ext="alias-ext")},
        "claude-x",
    )
    assert resp.status_code == 200
    assert _body(resp)["signed_by"] == "ASIA-111122223333"
    assert _body(resp)["region"] == PROFILE_REGION, "계정만 바뀌고 리전은 그 요청의 리전 그대로"
    assert [c["RoleArn"] for c in world.sts.calls] == [ALIAS_ROLE], "프로필 역할을 assume 했다"
    assert world.sts.calls[0]["ExternalId"] == "alias-ext"
    assert world.pod.invoke_model.call_count == 0


async def test_claude_code_alias_account_failure_is_500_and_never_falls_back(monkeypatch, world):
    """alias 계정 실패 = 고정 500. in-account 투명 폴백도, 프로필 계정 폴백도 하지 않는다."""
    world.sts._fail.add(ALIAS_ROLE)
    cb = _CB()
    resp, st = await _messages(
        monkeypatch,
        world,
        _claude_code_profile(),
        {"claude-x": _cfg("claude-x", role=ALIAS_ROLE)},
        "claude-x",
        cb=cb,
    )
    assert resp.status_code == ACCOUNT_UNAVAILABLE_STATUS
    assert _body(resp)["error"]["type"] == "api_error"
    assert world.pod.invoke_model.call_count == 0
    assert [c["RoleArn"] for c in world.sts.calls] == [ALIAS_ROLE]
    assert cb.failures == [], "계정 설정 오류가 모델 id 키의 서킷 브레이커를 올리면 안 된다"
    st.cost_recorder.finalize.assert_not_called()


# ── Bedrock native: 프로필이 native 에 적용되지 않는 앱 ──────────────────────────────


async def test_cowork_plain_bedrock_alias_without_account_stays_on_the_pod(monkeypatch, world):
    """cowork 의 역할은 Mantle 용이다(backend=mantle). 그 앱의 native 호출은 오늘 그대로 파드."""
    resp, st = await _messages(
        monkeypatch,
        world,
        _cowork_profile(default_model=None),
        {"claude-x": _cfg("claude-x")},
        "claude-x",
        client="cowork",
    )
    assert resp.status_code == 200 and _body(resp)["signed_by"] == "POD"
    assert world.sts.calls == []
    assert _finalize_kwargs(st)["profile_account"] is None


async def test_codex_alias_without_account_stays_on_the_pod(monkeypatch, world):
    # backend=mantle + default_model 이면 /v1/messages 는 Rule A(기본 모델 강제)로 Mantle 에 간다.
    # native 분기를 보려면 역할 없는 runtime 백엔드 모양을 쓴다(codex 의 다른 트랙).
    resp, _st = await _messages(
        monkeypatch,
        world,
        _codex_profile(backend="bedrock_openai"),
        {"claude-x": _cfg("claude-x")},
        "claude-x",
        client="codex",
    )
    assert _body(resp)["signed_by"] == "POD" and world.sts.calls == []


async def test_an_app_without_a_profile_reaches_an_alias_account(monkeypatch, world):
    resp, _st = await _messages(
        monkeypatch,
        world,
        None,
        {"claude-x": _cfg("claude-x", role=ALIAS_ROLE)},
        "claude-x",
        client="other",
    )
    assert _body(resp)["signed_by"] == "ASIA-111122223333"
    assert _body(resp)["region"] == POD_REGION, "프로필이 없으면 파드 리전"


# ── 폴백 후보는 자기 계정 ────────────────────────────────────────────────────────────


async def test_a_fallback_candidate_is_signed_for_its_own_account(monkeypatch, world):
    """원본(계정 없음)이 502 → 폴백 후보는 **그 후보의** alias 계정으로 서명된다."""
    world.registry.register(
        ProviderType.BEDROCK,
        BedrockAdapter(
            _fake_boto("POD", POD_REGION, fail_code="ServiceException"),
            account_clients=world.provider,
            account_region=POD_REGION,
        ),
    )
    configs = {"claude-a": _cfg("claude-a"), "claude-b": _cfg("claude-b", role=OTHER_ROLE)}
    resp, st = await _messages(
        monkeypatch,
        world,
        None,
        configs,
        "claude-a",
        client="other",
        fallback_order=["claude-a", "claude-b"],
        alias_map={"claude-a": ProviderType.BEDROCK, "claude-b": ProviderType.BEDROCK},
    )
    assert resp.status_code == 200
    assert _body(resp)["signed_by"] == "ASIA-777788889999"
    # 기록되는 모델 = 실제로 응답한 후보(그래서 0045 계정도 그 후보의 alias 계정이 된다).
    assert st.cost_recorder.finalize.await_args.args[2].alias == "claude-b"


async def test_an_alias_account_failure_does_not_trigger_the_fallback_chain(monkeypatch, world):
    """500 은 폴백 상태가 아니다 — 다른 계정으로 청구되는 후보가 대신 받으면 안 된다."""
    world.sts._fail.add(ALIAS_ROLE)
    configs = {"claude-a": _cfg("claude-a", role=ALIAS_ROLE), "claude-b": _cfg("claude-b")}
    resp, _st = await _messages(
        monkeypatch,
        world,
        None,
        configs,
        "claude-a",
        client="other",
        fallback_order=["claude-a", "claude-b"],
        alias_map={"claude-a": ProviderType.BEDROCK, "claude-b": ProviderType.BEDROCK},
    )
    assert resp.status_code == ACCOUNT_UNAVAILABLE_STATUS
    assert world.pod.invoke_model.call_count == 0, "폴백 후보(파드)로 넘어갔다"


# ── count_tokens 는 invoke 와 같은 계정 ─────────────────────────────────────────────


async def _count(monkeypatch, world, profile, cfg, client="claude-code"):
    import app.routers.messages as messages_mod

    monkeypatch.setattr(messages_mod, "_router_service", _Router({cfg.alias: cfg}))
    st = _app_state(world, profile)
    state = {
        "auth_context": None,
        "client": client,
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-1",
    }
    req = _FakeRequest(
        {"model": cfg.alias, "messages": [{"role": "user", "content": "x"}]}, state, st
    )
    resp = await messages_mod.count_tokens(req)
    return resp


async def test_count_tokens_follows_the_same_precedence(monkeypatch, world):
    # 계정 없음 + claude-code 프로필 → 프로필 계정(오늘): 키 길이로 누가 셌는지 본다.
    resp = await _count(monkeypatch, world, _claude_code_profile(), _cfg("claude-x"))
    assert _body(resp) == {"input_tokens": len("ASIA-444455556666")}
    # alias 계정 → 그 계정이 센다(프로필 resolver 를 거치지 않는다).
    world.sts.calls.clear()
    resp = await _count(
        monkeypatch, world, _claude_code_profile(), _cfg("claude-y", role=ALIAS_ROLE)
    )
    assert _body(resp) == {"input_tokens": len("ASIA-111122223333")}
    assert [c["RoleArn"] for c in world.sts.calls] == [ALIAS_ROLE]
    # 프로필이 native 에 적용되지 않는 앱 + 계정 없음 → 파드.
    resp = await _count(monkeypatch, world, _cowork_profile(), _cfg("claude-z"), client="cowork")
    assert _body(resp) == {"input_tokens": len("POD")}


# ── /model/* 패스스루 ───────────────────────────────────────────────────────────────


async def _passthrough(monkeypatch, world, cfg, model_ref=None):
    import app.routers.bedrock as bedrock_mod

    monkeypatch.setattr(bedrock_mod, "_router_service", _Router({model_ref or cfg.alias: cfg}))
    st = _app_state(world, _claude_code_profile())
    state = {
        "auth_context": None,
        "client": "claude-code",
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-1",
    }
    req = _FakeRequest({"messages": []}, state, st)
    resp = await bedrock_mod._handle_bedrock(req, model_ref or cfg.alias, "invoke", stream=False)
    return resp, st


async def test_passthrough_never_consults_the_profile_but_honours_the_alias_account(
    monkeypatch, world
):
    resp, st = await _passthrough(monkeypatch, world, _cfg("claude-x"))
    assert _body(resp)["signed_by"] == "POD", "패스스루는 오늘처럼 프로필을 보지 않는다"
    st.routing_profile_loader.load.assert_not_called()
    resp, _st = await _passthrough(monkeypatch, world, _cfg("claude-y", role=ALIAS_ROLE))
    assert _body(resp)["signed_by"] == "ASIA-111122223333"
    assert _body(resp)["region"] == POD_REGION


async def test_passthrough_maps_an_unknown_model_to_404(monkeypatch, world):
    import app.routers.bedrock as bedrock_mod

    monkeypatch.setattr(bedrock_mod, "_router_service", _Router({}))
    st = _app_state(world, None)
    req = _FakeRequest({}, {"auth_context": None, "_redis": None, "_session_factory": None}, st)
    resp = await bedrock_mod._handle_bedrock(req, "global.anthropic.nope", "invoke", stream=False)
    assert resp.status_code == 404


# ── Mantle (/v1/messages Rule B) ────────────────────────────────────────────────────


async def test_mantle_alias_without_account_hands_the_broker_the_profile_only(monkeypatch, world):
    broker = MagicMock()
    broker.bearer_token = AsyncMock(return_value="tok")
    http = MagicMock()
    http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            content=json.dumps({"usage": {"input_tokens": 1, "output_tokens": 1}}).encode(),
        )
    )
    world.registry.register(
        ProviderType.BEDROCK_MANTLE, MantleAdapter(http_client=http, broker=broker)
    )
    profile = _cowork_profile(default_model=None)
    mantle = _cfg(
        "cowork-opus",
        provider=ProviderType.BEDROCK_MANTLE,
        pmid="anthropic.claude-opus-4-8",
        endpoint="https://bedrock-mantle.ap-northeast-1.api.aws/anthropic",
    )
    resp, st = await _messages(
        monkeypatch, world, profile, {"cowork-opus": mantle}, "cowork-opus", client="cowork"
    )
    assert resp.status_code == 200
    assert broker.bearer_token.await_args.args == (profile,), (
        "계정 없는 Mantle 호출의 인자가 바뀌었다"
    )
    assert _finalize_kwargs(st)["profile_account"] == AccountTarget(
        PROFILE_ROLE, "cowork-ext", "profile"
    )

    with_acct = mantle.model_copy(update={"account_role_arn": ALIAS_ROLE})
    resp, _st = await _messages(
        monkeypatch, world, profile, {"cowork-opus": with_acct}, "cowork-opus", client="cowork"
    )
    assert broker.bearer_token.await_args.args == (profile, AccountTarget(ALIAS_ROLE))


# ── bedrock-runtime OpenAI (/v1/responses, /v1/chat/completions) ────────────────────


def _runtime_world(world):
    signer = MagicMock()
    signer.sign = AsyncMock(return_value={"Authorization": "sig"})
    http = MagicMock()
    http.post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            headers={},
            content=json.dumps(
                {"usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}
            ).encode(),
        )
    )
    world.registry.register(
        ProviderType.BEDROCK_RUNTIME_OPENAI, BedrockOpenAIAdapter(http_client=http, signer=signer)
    )
    return signer


async def _openai(monkeypatch, world, profile, cfg, path):
    import app.routers.openai_compat as oc

    monkeypatch.setattr(oc, "_router_service", _Router({cfg.alias: cfg}))
    st = _app_state(world, profile)
    state = {
        "auth_context": _auth(),
        "client": profile.client if profile else "other",
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-1",
    }
    req = _FakeRequest({"model": cfg.alias, "input": "hi", "messages": []}, state, st)
    if path == "responses":
        return await oc._handle_responses(req), st
    return await oc._handle_openai(req, "/v1/chat/completions"), st


@pytest.mark.parametrize("path", ["responses", "chat"])
async def test_runtime_alias_without_account_signs_with_the_profile_role_as_today(
    monkeypatch, world, path
):
    signer = _runtime_world(world)
    profile = RoutingProfileSchema(
        client="codex",
        backend="bedrock_openai",
        account_role_arn=PROFILE_ROLE,
        region=POD_REGION,
        default_model="gpt-x",
        external_id="codex-ext",
    )
    cfg = _cfg(
        "gpt-x",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        pmid="us.openai.gpt-x",
        endpoint=RUNTIME_ENDPOINT,
    )
    resp, st = await _openai(monkeypatch, world, profile, cfg, path)
    assert resp.status_code == 200
    kw = signer.sign.await_args.kwargs
    assert "account" not in kw
    assert (kw["role_arn"], kw["external_id"]) == (PROFILE_ROLE, "codex-ext")
    assert _finalize_kwargs(st)["profile_account"] == AccountTarget(
        PROFILE_ROLE, "codex-ext", "profile"
    )


@pytest.mark.parametrize("path", ["responses", "chat"])
async def test_runtime_alias_account_is_handed_to_the_signer(monkeypatch, world, path):
    signer = _runtime_world(world)
    profile = RoutingProfileSchema(
        client="codex",
        backend="bedrock_openai",
        account_role_arn=PROFILE_ROLE,
        region=POD_REGION,
        default_model="gpt-x",
        external_id="codex-ext",
    )
    cfg = _cfg(
        "gpt-x",
        provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
        pmid="us.openai.gpt-x",
        endpoint=RUNTIME_ENDPOINT,
        role=ALIAS_ROLE,
        ext="alias-ext",
    )
    resp, _st = await _openai(monkeypatch, world, profile, cfg, path)
    assert resp.status_code == 200
    assert signer.sign.await_args.kwargs["account"] == AccountTarget(ALIAS_ROLE, "alias-ext")
