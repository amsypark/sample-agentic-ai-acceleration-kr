# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""rate-limit 실시간 사용량의 SCAN 패턴을 **실 Redis** 로 증명한다 (REDIS_PROOF_URL).

단위 테스트(test_rate_limit_live_usage.py)는 Redis glob 을 정규식으로 흉내 낸다. 그 흉내가
실제 Redis 의 ``stringmatchlen`` 과 같다는 보장은 없다 — 이 파일이 그 간극을 닫는다.

  * ``[1m]`` 은 glob 문자 클래스다. 이스케이프하지 않은 패턴은 ``claude-…[1m]`` 키와
    **매치되지 않는다**(대조군으로 직접 SCAN 해 보인다). 그래서 콘솔이 0 을 보여줬다.
  * GLOBAL 키의 scope_id 자리는 리터럴 ``*`` 이고, ``[*]`` 는 그 리터럴만 잡는다.
  * 한 모델의 GLOBAL 사용량에 다른 모델이 섞이지 않는다. URL 의 scope_id 에 ``*`` 를 넣어도
    남의 카운터를 합산하지 않는다.

키는 gateway-proxy 의 실제 빌더(``rate_limit_scope.build_rl_key``)로 만든다 — 손으로 쓴 키는
두 배포물의 규약이 갈라져도 통과한다.

실행::

    docker run -d --name redis-proof -p 56379:6379 redis:7-alpine
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 pytest \\
        tests/regression/test_high_rate_limit_live_usage_real_redis.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
PROOF_DSN = os.environ.get("PROOF_DSN")  # 설정돼 있으면 증명 잡이다 — 그때 Redis 가 없으면 실패
PROXY_SCOPE_MODULE = (
    Path(__file__).resolve().parents[3] / "gateway-proxy" / "src" / "app" / "services" / "rate_limit_scope.py"
)

# ⚠️ 둘 다 없을 때(일반 pytest·로컬)만 skip 한다. CI 의 pg-proof 잡은 PROOF_DSN 과 REDIS_PROOF_URL 을
#    함께 준다 — PROOF_DSN 만 있는데 Redis 가 빠지면 아래 픽스처가 **실패**시킨다. 조용히 skip 되면
#    이 증명이 CI 에서 사라진 것을 아무도 모른다(GitHub Actions 는 모든 잡에 CI=true 를 주므로 그
#    변수로는 증명 잡을 가릴 수 없다).
pytestmark = pytest.mark.skipif(
    not REDIS_PROOF_URL and not PROOF_DSN,
    reason="REDIS_PROOF_URL 미설정 — glob 매칭은 실 Redis 에서만 증명된다",
)


def _proxy():
    assert PROXY_SCOPE_MODULE.exists(), f"gateway-proxy 키 빌더를 찾지 못했다: {PROXY_SCOPE_MODULE}"
    name = "proxy_rate_limit_scope_real_redis"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PROXY_SCOPE_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def redis_client():
    if not REDIS_PROOF_URL:
        pytest.fail("PROOF_DSN 은 있는데 REDIS_PROOF_URL 이 없다 — 이 증명은 실 Redis 가 필요하다")
    redis_asyncio = pytest.importorskip("redis.asyncio", reason="redis 패키지 필요")
    client = redis_asyncio.from_url(REDIS_PROOF_URL, decode_responses=True)
    assert await client.ping() is True, f"REDIS_PROOF_URL 에 붙지 못했다: {REDIS_PROOF_URL}"
    created: list[str] = []
    client.created = created  # 정리용
    yield client
    if created:
        await client.delete(*created)
    await client.aclose()


async def _zset(client, key: str, n: int) -> None:
    """gateway-proxy 처럼 member=request_id, score=now_ms 로 n건을 넣는다."""
    now_ms = int(time.time() * 1000)
    await client.zadd(key, {f"req-{uuid.uuid4().hex}": now_ms - i for i in range(n)})
    client.created.append(key)


def _service(client):
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.rate_limit_service import RateLimitService

    return RateLimitService(cache_mgr=CacheInvalidationManager(client))


async def test_bracket_alias_is_found_only_when_escaped(redis_client):
    proxy = _proxy()
    # 테스트끼리·다른 데이터와 섞이지 않게 alias 에 고유 접미사를 둔다([1m] 은 유지).
    alias = f"claude-proof-{uuid.uuid4().hex[:8]}[1m]"
    key = proxy.build_rl_key(proxy.RateLimitScope.GLOBAL, None, alias, "rpm")
    await _zset(redis_client, key, 6)

    out = await _service(redis_client).get_live_usage("GLOBAL", alias)
    assert out["available"] is True
    assert out["rpm_used_total"] == 6
    assert out["by_model"] == [{"model_alias": alias, "rpm_used": 6}]

    # 대조군 — 이스케이프하지 않은 예전 모양의 패턴은 실 Redis 에서 0건이다.
    unescaped = f"{{GLOBAL:[*]:{alias}}}:rpm"
    found = [k async for k in redis_client.scan_iter(match=unescaped, count=1000)]
    assert found == [], f"이스케이프 없이도 매치됐다 — 이 테스트의 전제가 틀렸다: {found}"


async def test_global_usage_is_per_model(redis_client):
    proxy = _proxy()
    tag = uuid.uuid4().hex[:8]
    mine, other = f"proof-a-{tag}", f"proof-b-{tag}"
    await _zset(redis_client, proxy.build_rl_key(proxy.RateLimitScope.GLOBAL, None, mine, "rpm"), 4)
    await _zset(redis_client, proxy.build_rl_key(proxy.RateLimitScope.GLOBAL, None, other, "rpm"), 9)

    out = await _service(redis_client).get_live_usage("GLOBAL", mine)
    assert out["rpm_used_total"] == 4, "다른 모델의 GLOBAL 카운터가 섞였다"


async def test_user_wildcard_scope_id_sums_nothing(redis_client):
    proxy = _proxy()
    uid = str(uuid.uuid4())
    await _zset(redis_client, proxy.build_rl_key(proxy.RateLimitScope.USER, uid, "proof-model", "rpm"), 3)

    svc = _service(redis_client)
    assert (await svc.get_live_usage("USER", uid))["rpm_used_total"] == 3  # 대조군 — 자기 것은 보인다
    assert (await svc.get_live_usage("USER", "*"))["rpm_used_total"] == 0


async def test_colon_in_alias_survives_key_parsing(redis_client):
    proxy = _proxy()
    uid = str(uuid.uuid4())
    alias = "anthropic.claude-proof-v1:0"
    await _zset(redis_client, proxy.build_rl_key(proxy.RateLimitScope.USER, uid, alias, "rpm"), 2)

    out = await _service(redis_client).get_live_usage("USER", uid)
    assert out["by_model"] == [{"model_alias": alias, "rpm_used": 2}]
