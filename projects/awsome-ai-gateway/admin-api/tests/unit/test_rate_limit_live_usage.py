# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

r"""``GET /admin/rate-limits/usage/{scope}/{scope_id}`` — '측정된 0' 과 '카운터 없음' 은
같은 응답이어서는 안 된다.

배경:
    gateway-proxy 는 **자신의 rpm 한도가 양수인 스코프에만** ZSET 을 만든다
    (``services/rate_limit_service.py`` ``check_multi_scope_rpm``: ``[d for d in descriptors
    if d.rpm_limit and d.rpm_limit > 0]``). 한도가 없는 사용자/팀은 초당 수천 건을 보내도
    ``{SCOPE:sid:model}:rpm`` 키가 아예 생기지 않는다. 그런데 이 엔드포인트는 키가 0건이어도
    ``{"available": true, "rpm_used_total": 0}`` 을 돌려줬고, 화면은 "최근 60초간 요청 없음"
    을 **단정**했다 — 그 문장을 읽는 대상이 "이 사용자에게 한도를 걸어야 하나" 를 판단하려는
    운영자다. 그래서 ``measured`` + ``limit_rpm`` 을 응답에 추가한다.

두 번째 결함(같은 함수): 키 패턴의 리터럴 이스케이프.
    ``claude-sonnet-4-6[1m]`` 은 실재하는 alias 이고 ``[1m]`` 은 glob 문자 클래스다 —
    이스케이프하지 않으면 자기 키와 매치되지 않아 조용히 0 이다(실 Redis 증명은
    test_high_rate_limit_live_usage_real_redis.py). GLOBAL 노드가 트리에 생기면서 GLOBAL 조회의
    scope_id 는 **model alias** 가 됐다 — 한 모델의 GLOBAL 사용량에 다른 모델을 합산하지 않는다.

키 모양은 gateway-proxy 의 **실제 함수**로 검증한다. ``rate_limit_scope.py`` 는 app 내부
import 가 없는 자립 모듈이라 파일 경로로 로드할 수 있다. 텍스트로 베끼면 두 배포물이 조용히
갈라지고, 그 결과가 바로 '언제나 0' 이다.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.cache_invalidation import CacheInvalidationManager
from app.models.model import RateLimitScope
from app.services.rate_limit_service import RateLimitService

# tests/unit/x.py -> tests/unit -> tests -> admin-api -> awsome-ai-gateway
PROJECT = Path(__file__).resolve().parents[3]
PROXY_SCOPE_MODULE = PROJECT / "gateway-proxy" / "src" / "app" / "services" / "rate_limit_scope.py"

USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
ALIAS = "claude-opus-4-7"
BRACKET_ALIAS = "claude-sonnet-4-6[1m]"


def load_proxy_scope() -> ModuleType:
    """gateway-proxy 의 키 빌더를 원본 그대로 로드 (없는 체크아웃이면 skip)."""
    if not PROXY_SCOPE_MODULE.exists():
        pytest.skip(f"sibling gateway-proxy not in this checkout: {PROXY_SCOPE_MODULE}")
    name = "proxy_rate_limit_scope_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PROXY_SCOPE_MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def redis_glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Redis ``stringmatchlen`` 의 glob 하위집합 → 정규식.

    ``fnmatch`` 를 쓸 수 없다: 백슬래시 이스케이프를 지원하지 않는다. Redis 는 지원한다.
    (이 에뮬레이션이 실제 Redis 와 같은지는 실 Redis 테스트가 따로 증명한다.)
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
            else:
                out.append(f"[{re.escape(pattern[i + 1:close])}]")
                i = close + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def fake_redis(zsets: dict[str, int]) -> AsyncMock:
    """scan_iter/zcount 만 흉내내는 대역. zsets = {key: 윈도우 내 항목 수}."""
    redis = AsyncMock()
    captured: list[str] = []

    def _scan_iter(*, match: str, count: int = 200):
        captured.append(match)
        rx = redis_glob_to_regex(match)

        async def _gen():
            for key in zsets:
                if rx.match(key):
                    yield key

        return _gen()

    redis.scan_iter = MagicMock(side_effect=_scan_iter)
    redis.zcount = AsyncMock(side_effect=lambda key, *_a: zsets.get(key, 0))
    redis.patterns = captured  # 테스트가 읽는다
    return redis


def service(zsets: dict[str, int]) -> tuple[RateLimitService, AsyncMock]:
    redis = fake_redis(zsets)
    return RateLimitService(cache_mgr=CacheInvalidationManager(redis)), redis


def session_with_configs(*rows) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    session.execute = AsyncMock(return_value=result)
    return session


def rl_row(*, scope_id=None, model_alias=None, rpm=None):
    return SimpleNamespace(scope_id=scope_id, model_alias=model_alias, rpm_limit=rpm, is_active=True)


class TestMeasuredVsMeasuredZero:
    async def test_no_counter_and_no_limit_is_not_measured(self):
        """키 0건 + 자기 한도 없음 → 측정 불가. 화면은 '요청 없음' 을 말해선 안 된다."""
        svc, _ = service({})
        out = await svc.get_live_usage("USER", str(USER_ID), session=session_with_configs())

        assert out["available"] is True
        assert out["measured"] is False
        assert out["limit_rpm"] is None
        assert out["rpm_used_total"] == 0

    async def test_limit_set_but_window_empty_is_measured_zero(self):
        """한도가 있으면 proxy 가 ZSET 을 만든다 → 0 은 진짜 '요청 없음'."""
        svc, _ = service({})
        out = await svc.get_live_usage(
            "USER", str(USER_ID), session=session_with_configs(rl_row(scope_id=USER_ID, rpm=60))
        )
        assert out["measured"] is True
        assert out["limit_rpm"] == 60
        assert out["rpm_used_total"] == 0

    async def test_unmeasured_and_measured_zero_are_distinguishable(self):
        """이 두 응답이 byte-identical 이던 것이 결함의 핵심이었다."""
        svc, _ = service({})
        unmeasured = await svc.get_live_usage("USER", str(USER_ID), session=session_with_configs())
        measured = await svc.get_live_usage(
            "USER", str(USER_ID), session=session_with_configs(rl_row(scope_id=USER_ID, rpm=60))
        )
        assert unmeasured != measured

    async def test_live_counter_wins_even_without_a_limit_row(self):
        """카운터가 실제로 잡혔다면(예: 모델별 한도) 한도 조회 결과와 무관하게 측정된 것."""
        svc, _ = service({f"{{USER:{USER_ID}:{ALIAS}}}:rpm": 7})
        out = await svc.get_live_usage("USER", str(USER_ID), session=session_with_configs())

        assert out["measured"] is True
        assert out["rpm_used_total"] == 7
        assert out["by_model"] == [{"model_alias": ALIAS, "rpm_used": 7}]

    async def test_all_model_row_wins_over_per_model_rows(self):
        """model_alias IS NULL 행은 모든 모델을 지배하므로 limit_rpm 은 그 값이다."""
        svc, _ = service({})
        out = await svc.get_live_usage(
            "USER",
            str(USER_ID),
            session=session_with_configs(
                rl_row(scope_id=USER_ID, model_alias="m1", rpm=500),
                rl_row(scope_id=USER_ID, model_alias=None, rpm=40),
            ),
        )
        assert out["limit_rpm"] == 40

    async def test_limit_query_is_scoped_to_this_subject_only(self):
        """USER 행이 없으면 USER ZSET 도 없다 — 팀 한도는 TEAM 키에 쌓인다. 상속을 끌어오면
        카운터가 없는 사용자를 measured=true 로 만들어 결함이 되살아난다."""
        svc, _ = service({})
        session = session_with_configs()
        await svc.get_live_usage("USER", str(USER_ID), session=session)

        params = session.execute.call_args.args[0].compile().params
        assert RateLimitScope.USER in params.values()
        assert USER_ID in params.values()
        assert RateLimitScope.TEAM not in params.values()

    async def test_global_limit_query_is_per_model_alias(self):
        svc, _ = service({})
        session = session_with_configs()
        await svc.get_live_usage("GLOBAL", ALIAS, session=session)

        stmt = session.execute.call_args.args[0]
        assert ALIAS in stmt.compile().params.values()
        assert "scope_id IS NULL" in str(stmt)

    async def test_all_null_user_row_is_not_measured(self):
        """전부 NULL 인 USER 행(= 필드를 비우고 저장한 결과)도 카운터를 만들지 않는다."""
        svc, _ = service({})
        out = await svc.get_live_usage(
            "USER", str(USER_ID), session=session_with_configs(rl_row(scope_id=USER_ID, rpm=None))
        )
        assert out["measured"] is False

    async def test_limit_lookup_failure_does_not_break_usage(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        svc, _ = service({f"{{USER:{USER_ID}:{ALIAS}}}:rpm": 3})

        out = await svc.get_live_usage("USER", str(USER_ID), session=session)

        assert out["available"] is True
        assert out["rpm_used_total"] == 3
        assert out["measured"] is True  # 카운터가 잡혔으므로
        assert out["limit_rpm"] is None

    async def test_non_uuid_scope_id_is_not_measured_and_does_not_raise(self):
        svc, _ = service({})
        out = await svc.get_live_usage("USER", "not-a-uuid", session=session_with_configs())
        assert out["available"] is True and out["measured"] is False

    async def test_invalid_scope_is_never_measured(self):
        svc, _ = service({})
        out = await svc.get_live_usage("DEPT", "x")
        assert out == {"available": False, "measured": False, "reason": "invalid scope"}

    async def test_redis_failure_is_never_measured(self):
        svc, redis = service({})
        redis.scan_iter = MagicMock(side_effect=RuntimeError("redis down"))
        out = await svc.get_live_usage(
            "USER", str(USER_ID), session=session_with_configs(rl_row(scope_id=USER_ID, rpm=60))
        )
        assert out["available"] is False
        assert out["measured"] is False


class TestKeyPatternParityWithGatewayProxy:
    """스캔 패턴이 proxy 가 실제로 쓰는 키와 맞물리는가. 어긋나면 결과는 '언제나 0'."""

    def _proxy_key(self, scope_name: str, scope_id: str | None, alias: str) -> str:
        proxy = load_proxy_scope()
        return proxy.build_rl_key(proxy.RateLimitScope(scope_name), scope_id, alias, "rpm")

    @pytest.mark.parametrize("scope_name", ["USER", "TEAM"])
    async def test_user_team_pattern_matches_proxy_key(self, scope_name: str):
        sid = str(USER_ID)
        key = self._proxy_key(scope_name, sid, ALIAS)
        svc, redis = service({key: 5})

        out = await svc.get_live_usage(scope_name, sid)

        assert redis_glob_to_regex(redis.patterns[0]).match(key)
        assert out["rpm_used_total"] == 5

    async def test_global_pattern_matches_proxy_key(self):
        """proxy 는 GLOBAL 의 scope_id 자리에 리터럴 '*' 를 쓴다. GLOBAL 조회의 scope_id 인자는
        트리의 GLOBAL 노드 id, 즉 model alias 다."""
        proxy = load_proxy_scope()
        key = self._proxy_key("GLOBAL", None, ALIAS)
        assert f":{proxy.GLOBAL_WILDCARD}:" in key  # 규약 자체를 고정

        svc, redis = service({key: 11})
        out = await svc.get_live_usage("GLOBAL", ALIAS)

        assert redis_glob_to_regex(redis.patterns[0]).match(key)
        assert out["rpm_used_total"] == 11
        assert out["by_model"] == [{"model_alias": ALIAS, "rpm_used": 11}]

    async def test_global_pattern_is_per_model(self):
        """한 모델의 GLOBAL 사용량 조회가 다른 모델 카운터를 합산해선 안 된다."""
        svc, _ = service(
            {
                self._proxy_key("GLOBAL", None, ALIAS): 4,
                self._proxy_key("GLOBAL", None, "other-model"): 99,
            }
        )
        out = await svc.get_live_usage("GLOBAL", ALIAS)
        assert out["rpm_used_total"] == 4

    async def test_alias_glob_metacharacters_are_escaped(self):
        key = self._proxy_key("GLOBAL", None, BRACKET_ALIAS)
        svc, redis = service({key: 6})

        out = await svc.get_live_usage("GLOBAL", BRACKET_ALIAS)

        pattern = redis.patterns[0]
        assert "\\[" in pattern and "\\]" in pattern
        assert redis_glob_to_regex(pattern).match(key)
        assert out["rpm_used_total"] == 6

    async def test_scope_id_wildcard_does_not_sum_other_subjects(self):
        """URL 의 scope_id 는 사용자 입력이다 — '*' 를 넣어도 남의 카운터를 합산하지 않는다."""
        other = uuid.uuid4()
        svc, _ = service(
            {
                self._proxy_key("USER", str(USER_ID), ALIAS): 2,
                self._proxy_key("USER", str(other), ALIAS): 9,
            }
        )
        out = await svc.get_live_usage("USER", "*")
        assert out["rpm_used_total"] == 0

    async def test_model_alias_with_colon_is_preserved(self):
        """키 파싱이 ':' 로 모두 쪼개면 alias 안의 ':' 이후가 잘린다(Bedrock id 는 `-v1:0`)."""
        alias = "anthropic.claude-x-v1:0"
        svc, _ = service({self._proxy_key("USER", str(USER_ID), alias): 2})
        out = await svc.get_live_usage("USER", str(USER_ID))
        assert out["by_model"] == [{"model_alias": alias, "rpm_used": 2}]
