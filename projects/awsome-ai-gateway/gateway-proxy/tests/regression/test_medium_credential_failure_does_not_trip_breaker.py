# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""자격증명 단계의 실패가 모델의 서킷 브레이커를 올리지 않는다.

무엇이 문제였나
---------------
라우팅 프로필 X 의 STS 가 멈추거나 신뢰 정책이 깨지면 그 프로필의 Mantle 요청은 502 를 받는다 —
첫 요청은 STS 가 실패한 뒤, 그다음 5초는 ``ProfileAssumeBackoff`` 로 곧바로. 상류는 한 번도
호출되지 않는다. 그런데 폴백 루프는 502 를 서킷 브레이커 실패로 셌고, 브레이커 키는 모델 id
(alias 계정이 있으면 ``@계정``)라 프로필을 모른다. 그래서 X 의 요청 다섯 번이면 그 모델의 브레이커가
열려, 자격증명이 멀쩡한 프로필 Y 의 같은 모델 요청이 30초 동안 503 "All fallback models are
temporarily unavailable" 을 받았다. 파드 계정 폴백이 없는 claude-code native 경로도 같다.

고친 것
-------
어댑터가 상류를 부르기 전 자격증명 단계에서 실패하면 돌려주는 headers 에 표지를 넣고, 폴백 루프는
그 실패를 브레이커에 기록하지 않는다. 상태 코드·본문·폴백 동작은 그대로다. 상류의 502 는 여전히
센다 — 그것은 모델(계정)의 건강이다.

이 파일의 테스트는 **브레이커 상태만** 본다(표지 이름을 import 하지 않는다). 그래서 고치기 전
코드에 그대로 돌려 대조군으로 쓸 수 있다. 브레이커는 실물(``CircuitBreakerService`` + 실 Redis)이고,
대역은 STS·bearer 생성기·Mantle HTTP 뿐이다.

⚠️ 단일 노드 Redis 에서만 돈다. 브레이커의 세 키(``cb:<pmid>:fail|total|open``)에는 hash tag 가
없어 Redis Cluster 에서는 기록 EVAL 이 클라이언트에서 거부되고(``RedisClusterException``: all keys
must map to the same key slot) ``cb_record_failed`` 로 삼켜진다 — 클러스터에서는 브레이커가 아예
열리지 않는다(실 클러스터 실측). 그 자체는 이 수정의 범위 밖이고, 클러스터 변형은 열리지 않는
브레이커를 "닫혀 있다" 로 통과시키는 공허한 테스트가 된다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from botocore.exceptions import ConnectTimeoutError

from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.mantle_adapter import MantleAdapter
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.schemas.routing import RoutingProfileSchema
from app.services import mantle_credentials as mc
from app.services.account_credentials import AccountCredentialsUnavailable, AccountTarget
from app.services.circuit_breaker import CircuitBreakerService
from app.services.fallback_loop import run_fallback_loop

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

ROLE_X = "arn:aws:iam::444455556666:role/app-x"
ROLE_Y = "arn:aws:iam::111122223333:role/app-y"
ENDPOINT = "https://bedrock-mantle.us-west-2.api.aws/anthropic"
#: 브레이커가 열리는 데 필요한 것보다 넉넉히(min_calls 5, 오류율 0.5).
REQUESTS = 8


@pytest.fixture
async def real_redis():
    if not REDIS_PROOF_URL:
        pytest.skip("REDIS_PROOF_URL 미설정 — 실 Redis 필요(브레이커 상태는 Redis Lua 다).")
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_PROOF_URL, socket_timeout=5)
    try:
        yield client
    finally:
        await client.aclose()


class _STS:
    """역할 X 는 실패한다(멈춘 STS 에 botocore 타임아웃이 걸린 모양), 그 밖은 1시간 자격증명."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.calls: list[str] = []

    def assume_role(self, **kw):
        self.calls.append(kw["RoleArn"])
        if kw["RoleArn"] == ROLE_X:
            self.clock[0] += 35.0  # STS 안에서 쓴 시간 — 그 뒤 기억 5초 안에 머문다
            raise ConnectTimeoutError(endpoint_url="https://sts.us-west-2.amazonaws.com")
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{len(self.calls)}",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": dt.datetime.fromtimestamp(self.clock[0] + 3600, dt.UTC),
            }
        }


class _Upstream:
    """Mantle HTTP 대역. 몇 번 불렸는지 센다 — 자격증명 실패는 상류를 부르지 않아야 한다."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "upstream says no"})
        if json.loads(request.content or b"{}").get("stream"):
            sse = 'data: {"type":"message_start"}\n\ndata: {"type":"message_stop"}\n\n'
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )


def _profile(role: str) -> RoutingProfileSchema:
    return RoutingProfileSchema(
        client="cowork",
        backend="mantle",
        account_role_arn=role,
        region="us-west-2",
        default_model=None,
        external_id="ext",
        enabled=True,
    )


def _model(pmid: str, provider: ProviderType) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=pmid,
        alias=f"alias-{pmid}",
        provider=provider,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )


async def _call(redis, cb, adapter, cfg, kwargs: dict, *, stream: bool):
    """폴백 루프 한 번(후보 = 원본 하나 — Mantle 은 같은 프로바이더 후보가 없다).

    auth_context 가 None 이면 입장 검사(스코프·한도)를 건너뛴다 — 여기서 보려는 것은 브레이커뿐이다.
    """
    result = await run_fallback_loop(
        try_order=[cfg.alias],
        original_alias=cfg.alias,
        is_stream=stream,
        req_data={"model": cfg.alias, "messages": [], "max_tokens": 16},
        redis=redis,
        auth_context=None,
        state={},
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        budget_status=None,
        adapter=adapter,
        stream_kwargs=kwargs,
        nonstream_kwargs=kwargs,
        cb=cb,
        router_service=MagicMock(),
        session_factory=None,
        is_db_degraded=True,
        original_model_config=cfg,
        resolve_model_config=None,  # 후보가 원본 하나라 부르지 않는다
        build_candidate_body=lambda req, c, s: (
            json.dumps({**req, "stream": s}).encode(),
            kwargs,
            kwargs,
        ),
        rewrite_model_id=lambda pmid: pmid,
    )
    if stream and result.status == 200:
        async for _ in result.payload[0]:  # 스트림을 끝까지 읽어 닫는다
            pass
    return result


async def _cleanup(redis, pmid: str) -> None:
    await redis.delete(*(f"cb:{pmid}:{s}" for s in ("fail", "total", "open", "halfopen")))


def _mantle(monkeypatch, upstream: _Upstream):
    class _Gen:
        def get_token(self, creds, region):
            return f"BEARER-{creds.access_key}-{region}"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())
    clock = [1000.0]
    sts = _STS(clock)
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: clock[0])
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return MantleAdapter(http_client=http, broker=broker), sts, http


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
async def test_a_failing_sts_on_profile_x_does_not_open_the_breaker_for_profile_y(
    real_redis, monkeypatch, stream
):
    """프로필 X 의 STS 실패(첫 요청)와 그 뒤의 ``ProfileAssumeBackoff`` 로 X 의 요청 여덟 번이
    전부 502 여도 같은 모델의 브레이커는 닫혀 있고, 프로필 Y 의 요청은 서빙된다. 상류는 Y 의 한
    번만 불린다. 고치기 전 코드에서는 다섯 번째 X 요청에서 브레이커가 열려, 여섯 번째(half-open
    시험 요청) 뒤로는 X 도 Y 도 상류에 가 보지도 못하고 503 이었다."""
    pmid = f"anthropic.cb-cred-{uuid.uuid4().hex[:8]}"
    cfg = _model(pmid, ProviderType.BEDROCK_MANTLE)
    upstream = _Upstream()
    adapter, sts, http = _mantle(monkeypatch, upstream)
    cb = CircuitBreakerService()
    try:
        x = {"profile": _profile(ROLE_X), "endpoint": ENDPOINT}
        statuses = [
            (await _call(real_redis, cb, adapter, cfg, x, stream=stream)).status
            for _ in range(REQUESTS)
        ]
        assert statuses == [502] * REQUESTS, statuses
        assert sts.calls == [ROLE_X], "기억(5초) 안의 X 요청은 STS 를 다시 부르지 않는다"
        assert upstream.calls == 0, "자격증명 실패는 상류를 부르지 않는다"
        assert not await cb.is_open(real_redis, pmid), "자격증명 실패가 모델의 브레이커를 열었다"

        y = {"profile": _profile(ROLE_Y), "endpoint": ENDPOINT}
        served = await _call(real_redis, cb, adapter, cfg, y, stream=stream)
        assert served.status == 200 and not served.all_open, (served.status, served.payload[0])
        assert upstream.calls == 1
    finally:
        await http.aclose()
        await _cleanup(real_redis, pmid)


async def test_upstream_502s_still_open_the_breaker(real_redis, monkeypatch):
    """대조 — 이 하네스의 브레이커는 실제로 열린다. 상류(Mantle)가 502 를 주면 그것은 모델(계정)의
    건강이므로 지금도 세고, 다섯 번 뒤 브레이커가 열린다. 그다음 요청 하나는 half-open 시험으로
    상류에 가고(또 502), 그 뒤 요청은 상류를 부르지 않고 503 이다. 이것이 열리지 않으면 위 테스트의
    "닫혀 있다" 는 아무것도 증명하지 않는다."""
    pmid = f"anthropic.cb-up-{uuid.uuid4().hex[:8]}"
    cfg = _model(pmid, ProviderType.BEDROCK_MANTLE)
    upstream = _Upstream(status=502)
    adapter, _sts, http = _mantle(monkeypatch, upstream)
    cb = CircuitBreakerService()
    try:
        y = {"profile": _profile(ROLE_Y), "endpoint": ENDPOINT}
        for _ in range(5):
            assert (await _call(real_redis, cb, adapter, cfg, y, stream=False)).status == 502
        assert await cb.is_open(real_redis, pmid)
        probe = await _call(real_redis, cb, adapter, cfg, y, stream=False)
        assert probe.status == 502 and upstream.calls == 6  # half-open 시험 요청 하나
        blocked = await _call(real_redis, cb, adapter, cfg, y, stream=False)
        assert blocked.status == 503 and blocked.all_open
        assert upstream.calls == 6
    finally:
        await http.aclose()
        await _cleanup(real_redis, pmid)


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
async def test_native_profile_client_failure_without_a_pod_fallback_is_not_counted(
    real_redis, stream
):
    """claude-code native 경로는 프로필 계정 클라이언트를 못 얻으면 파드 계정으로 투명 폴백한다.
    그 폴백이 없을 때(파드 클라이언트 미배선)는 502 이고, 그것도 자격증명 실패라 세지 않는다."""
    pmid = f"anthropic.cb-native-{uuid.uuid4().hex[:8]}"
    cfg = _model(pmid, ProviderType.BEDROCK)

    async def _resolver():
        raise AccountCredentialsUnavailable(ROLE_X, "AssumeRole failed: ConnectTimeoutError")

    adapter = BedrockAdapter(bedrock_client=None, client_resolver=_resolver, fallback_client=None)
    cb = CircuitBreakerService()
    path = {"path_suffix": "invoke-with-response-stream" if stream else "invoke"}
    try:
        statuses = [
            (await _call(real_redis, cb, adapter, cfg, path, stream=stream)).status
            for _ in range(REQUESTS)
        ]
        assert statuses == [502] * REQUESTS, statuses
        assert not await cb.is_open(real_redis, pmid), "자격증명 실패가 모델의 브레이커를 열었다"
    finally:
        await _cleanup(real_redis, pmid)


async def test_an_unusable_alias_account_is_not_counted(real_redis):
    """alias 계정(0043)의 AssumeRole 실패는 처음부터 500 이다(폴백·브레이커 집합 밖) — 그대로다."""
    pmid = f"anthropic.cb-alias-{uuid.uuid4().hex[:8]}"
    cfg = _model(pmid, ProviderType.BEDROCK)

    class _Provider:
        async def get_account_client(self, target, region):
            raise AccountCredentialsUnavailable(target.role_arn, "AssumeRole failed")

    adapter = BedrockAdapter(
        bedrock_client=None, account_clients=_Provider(), account_region="us-west-2"
    )
    cb = CircuitBreakerService()
    kwargs = {"path_suffix": "invoke", "account": AccountTarget(role_arn=ROLE_X)}
    try:
        statuses = [
            (await _call(real_redis, cb, adapter, cfg, kwargs, stream=False)).status
            for _ in range(REQUESTS)
        ]
        assert statuses == [500] * REQUESTS, statuses
        assert not await cb.is_open(real_redis, pmid)
    finally:
        await _cleanup(real_redis, pmid)
