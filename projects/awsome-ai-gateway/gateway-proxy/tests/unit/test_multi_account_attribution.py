# Copyright 2026 © Amazon.com and Affiliates.

"""usage_logs.aws_account_id (마이그레이션 0045) — 요청을 **실제로 서빙한** 계정.

실패 모드는 오류가 아니라 틀린 값이다. 기록 규칙이 서명 규칙과 어긋나면 정산은 그 호출의
bedrock_request_id 를 엉뚱한 계정에서 찾고 "미매칭" 을 보고한다 — invocation logging 이 꺼진
것과 구별되지 않는다. 그래서 이 파일은 기록을 서명 규칙에 묶는다:

  * effective_account_id 의 표(경로·alias 계정·프로필 계정 조합),
  * finalize → cost stream payload,
  * 라우터의 모든 finalize 가 "그 경로가 실제로 서명에 쓴 프로필 계정" 을 넘기는지(AST),
  * 라우터의 모든 어댑터 호출이 계정을 실어 나르는 경로를 타는지(AST).

처리기를 통과하는 기록 검사(claude-code 폴백 = 파드로 기록 등)는
``test_multi_account_precedence.py`` 에 있다.
"""

from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.domain import ProviderType, TokenUsage
from app.services.account_credentials import (
    AccountTarget,
    account_id_of,
    effective_account_id,
)

ALIAS_ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
PROFILE = AccountTarget("arn:aws:iam::444455556666:role/app-bedrock", "ext", source="profile")
SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "app"
ROUTERS = SRC / "routers"


def _cfg(provider, role=None, ext=None):
    return SimpleNamespace(provider=provider, account_role_arn=role, account_external_id=ext)


@pytest.mark.parametrize(
    "provider, alias_role, profile_account, expected",
    [
        # alias 계정이 없으면 라우터가 넘긴(= 실제로 서명한) 프로필 계정, 없으면 게이트웨이 계정.
        (ProviderType.BEDROCK, None, None, None),
        (ProviderType.BEDROCK, None, PROFILE, "444455556666"),
        (ProviderType.BEDROCK_RUNTIME_OPENAI, None, PROFILE, "444455556666"),
        (ProviderType.BEDROCK_MANTLE, None, PROFILE, "444455556666"),
        (ProviderType.BEDROCK_MANTLE_OPENAI, None, None, None),
        # alias 계정은 모든 경로에서 프로필보다 먼저다.
        (ProviderType.BEDROCK, ALIAS_ROLE, PROFILE, "111122223333"),
        (ProviderType.BEDROCK_RUNTIME_OPENAI, ALIAS_ROLE, PROFILE, "111122223333"),
        (ProviderType.BEDROCK_MANTLE, ALIAS_ROLE, PROFILE, "111122223333"),
        (ProviderType.BEDROCK_MANTLE_OPENAI, ALIAS_ROLE, None, "111122223333"),
        # AWS 호출이 아니다 — 행에 역할이 잘못 남아 있어도 AWS 청구로 기록하지 않는다.
        (ProviderType.OPENMODEL, ALIAS_ROLE, PROFILE, None),
    ],
)
def test_attribution_follows_the_signing_precedence(
    provider, alias_role, profile_account, expected
):
    assert effective_account_id(_cfg(provider, alias_role), profile_account) == expected


def test_plain_string_providers_are_understood():
    """캐시·느슨한 설정은 enum 이 아니라 값 문자열을 들고 온다."""
    assert effective_account_id(_cfg("BEDROCK_MANTLE"), PROFILE) == "444455556666"
    assert effective_account_id(_cfg("OPENMODEL", ALIAS_ROLE)) is None


@pytest.mark.parametrize(
    "role", ["arn:aws:iam::12345:role/short", "arn:aws:iam::ABCDEFGHIJKL:role/x", "not-an-arn", ""]
)
def test_an_unparseable_role_records_no_account_rather_than_garbage(role):
    assert account_id_of(role) is None
    assert effective_account_id(_cfg(ProviderType.BEDROCK, role or None)) is None


def test_other_partitions_are_parsed():
    assert account_id_of("arn:aws-us-gov:iam::111122223333:role/x") == "111122223333"


# ── finalize → cost stream ─────────────────────────────────────────────────────────


def _mock_budget(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"
    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()


@pytest.mark.asyncio
async def test_finalize_puts_the_serving_account_on_the_cost_stream(
    mock_redis, auth_context_vk, model_config_bedrock
):
    from app.services.cost_recorder import CostRecorder

    _mock_budget(mock_redis)
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

    async def payload(model_config, profile_account=None):
        mock_redis.xadd.reset_mock()
        await CostRecorder().finalize(
            redis=mock_redis,
            auth_context=auth_context_vk,
            model_config=model_config,
            usage=usage,
            request_id="req-acct",
            is_stream=False,
            duration_ms=10,
            profile_account=profile_account,
        )
        return json.loads(mock_redis.xadd.call_args.args[1]["payload"])

    pinned = model_config_bedrock.model_copy(update={"account_role_arn": ALIAS_ROLE})
    assert (await payload(pinned))["aws_account_id"] == "111122223333"
    assert (await payload(model_config_bedrock, PROFILE))["aws_account_id"] == "444455556666"
    # 프로필 계정을 넘기지 않으면(= 그 경로가 프로필로 서명하지 않았다) 게이트웨이 계정.
    assert (await payload(model_config_bedrock))["aws_account_id"] is None


@pytest.mark.asyncio
async def test_an_attribution_failure_never_costs_the_billing_row(
    mock_redis, auth_context_vk, model_config_bedrock
):
    from app.services.cost_recorder import CostRecorder

    _mock_budget(mock_redis)
    with patch("app.services.cost_recorder.effective_account_id", side_effect=RuntimeError("boom")):
        await CostRecorder().finalize(
            redis=mock_redis,
            auth_context=auth_context_vk,
            model_config=model_config_bedrock,
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            request_id="req-x",
            is_stream=False,
            duration_ms=1,
        )
    payload = json.loads(mock_redis.xadd.call_args.args[1]["payload"])
    assert payload["request_id"] == "req-x" and payload["aws_account_id"] is None


def test_a_pre_0045_cost_stream_entry_still_parses():
    """롤아웃 순서 무관: 필드가 없는 엔트리도 그대로 읽힌다."""
    from app.schemas.cost_stream import CostStreamEntry

    e = CostStreamEntry.make(
        request_id="r",
        user_id="u",
        team_id="t",
        dept_id="d",
        model_alias="m",
        provider="BEDROCK",
        input_tokens=1,
        output_tokens=1,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd=0,
        latency_ms=1,
        is_streaming=False,
        estimated_usage=False,
        downgraded_from=None,
    )
    raw = e.model_dump(mode="json")
    raw.pop("aws_account_id")
    assert CostStreamEntry.model_validate(raw).aws_account_id is None


# ── 라우터 구조 고정 (AST) ─────────────────────────────────────────────────────────


def _calls(path: pathlib.Path, attr_names: set[str], receiver: str | None = None):
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in attr_names:
            continue
        recv = node.func.value
        name = getattr(recv, "id", None) or getattr(recv, "attr", None)
        if receiver is not None and name != receiver:
            continue
        out.append(node)
    return out


@pytest.mark.parametrize(
    "filename, expected",
    [
        # 웹서치 / 스트림 / 비스트림 — 셋 다 "그 요청이 실제로 서명에 쓴 프로필 계정" 을 넘긴다.
        ("messages.py", "_served_profile_account"),
        # /v1/chat/completions(runtime 만 프로필로 서명) 2곳 + /v1/responses 3곳.
        ("openai_compat.py", "served_profile_account"),
    ],
)
def test_every_bedrock_capable_finalize_passes_the_profile_account(filename, expected):
    """빠지면 cowork 의 Mantle 호출·claude-code 의 cross-account 호출이 "게이트웨이 계정" 으로
    기록된다 — 정산이 그 bedrock_request_id 를 엉뚱한 계정에서 찾는다."""
    calls = _calls(ROUTERS / filename, {"finalize"})
    assert calls, f"{filename}: finalize 호출을 하나도 못 찾았다 — 이 검사가 공허하다"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        assert "profile_account" in kw, (
            f"{filename}:{call.lineno} finalize() 에 profile_account= 가 없다"
        )
        src = ast.unparse(kw["profile_account"])
        assert expected in src, f"{filename}:{call.lineno} profile_account={src} (기대: {expected})"


@pytest.mark.parametrize("filename", ["messages.py", "bedrock.py", "openai_compat.py"])
def test_every_adapter_call_carries_the_account_path(filename):
    """**계정 kwarg 를 빠뜨린 새 어댑터 호출 지점**은 예외도 로그도 없이 파드/프로필 자격증명으로
    나가 다른 계정에 청구된다. 그래서 구조로 막는다: 모든 ``adapter.invoke*/count_tokens`` 는
    ``account=`` 이거나, 계정을 담아 만든 dict 의 ``**`` 펼침이거나, OpenModel(``path=``)이다.
    펼치는 dict 를 만드는 지점은 아래 동반 검사가 본다."""
    calls = _calls(
        ROUTERS / filename, {"invoke", "invoke_stream", "count_tokens"}, receiver="adapter"
    )
    assert calls, f"{filename}: 어댑터 호출을 하나도 못 찾았다 — 이 검사가 공허하다"
    offenders = []
    for call in calls:
        names = {k.arg for k in call.keywords}
        if "account" in names or None in names or "path" in names:
            continue
        offenders.append(call.lineno)
    assert not offenders, f"{filename}: 계정 경로가 없는 어댑터 호출 — 줄 {offenders}"


def test_the_candidate_builder_resolves_the_candidate_not_the_original():
    """폴백 후보는 다른 alias 이고 다른 계정일 수 있다. 원래 요청 alias(model_config)로 해석하면
    모든 폴백이 첫 alias 의 계정으로 청구된다 — 폴백은 이미 뭔가 잘못됐을 때 일어나므로 가장
    덜 시험되고 가장 잘못 청구되기 쉬운 경로다."""
    tree = ast.parse((ROUTERS / "messages.py").read_text(encoding="utf-8"))
    builder = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_candidate_body"
    )
    resolves = [
        n
        for n in ast.walk(builder)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_account_kwargs"
    ]
    assert len(resolves) == 2, (
        f"Mantle·Bedrock 두 분기 모두 후보 계정을 해석해야 한다({len(resolves)})"
    )
    assert all(getattr(c.args[0], "id", None) == "cand_config" for c in resolves)


def test_the_openai_handlers_put_the_alias_account_into_the_shared_kwargs():
    """/v1/responses 와 /v1/chat/completions 는 dict 하나를 모든 호출 지점(웹서치 클로저 포함)이
    펼친다 — 그 dict 에 계정이 들어가는 지점이 두 처리기 모두에 있어야 한다."""
    tree = ast.parse((ROUTERS / "openai_compat.py").read_text(encoding="utf-8"))
    for fn_name in ("_handle_openai", "_handle_responses"):
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name
        )
        sets = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript)
                and getattr(t.value, "id", None) == "invoke_kwargs"
                and getattr(t.slice, "value", None) == "account"
                for t in n.targets
            )
        ]
        assert sets, f"{fn_name}: invoke_kwargs['account'] 를 채우는 지점이 없다"


# ── /model/* 스트림 패스스루: 보호된 finalize(services/bg_tasks.py) 도 서명 계정을 기록한다 ──


@pytest.mark.asyncio
async def test_the_passthrough_stream_signs_and_records_the_alias_account_through_a_disconnect(
    monkeypatch, mock_redis, auth_context_vk, model_config_bedrock
):
    """서명은 alias 계정으로, 원장 행도 그 계정으로 — 클라이언트가 finalize 도중에 끊어도.

    끊김은 종결 청크 직후, finalize 가 예산 차감과 원장 XADD 사이에 있을 때 떨어뜨린다. finalize 가
    보호 태스크가 아니면 XADD 가 잘려 원장 행이 없다. 패스스루는 라우팅 프로필을 보지 않으므로
    profile_account 를 넘기지 않는다 — 기록 계정은 finalize 가 model_config(서명 계정을 고른 바로 그
    설정)에서 읽는다(account_credentials.effective_account_id).
    """
    import asyncio

    from app.routers import bedrock
    from app.services import rate_limit_enforcement
    from app.services.cost_recorder import CostRecorder

    _mock_budget(mock_redis)
    in_xadd, release, written = asyncio.Event(), asyncio.Event(), []

    async def _parked_xadd(stream, fields, *args, **kwargs):
        in_xadd.set()
        await release.wait()
        written.append(json.loads(fields["payload"]))

    mock_redis.xadd = AsyncMock(side_effect=_parked_xadd)
    model = model_config_bedrock.model_copy(update={"account_role_arn": ALIAS_ROLE})
    signed_with = []

    class _Adapter:
        async def invoke_stream(self, body, model_id, path_suffix=None, account=None):
            signed_with.append(account)

            async def chunks():
                usage = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
                yield f"data: {json.dumps({'usage': usage})}\n\n".encode()

            return 200, chunks(), {"Content-Type": "application/octet-stream"}, "rid"

    async def _resolve(*_a, **_k):
        return model

    async def _no_limit(**_k):
        return None

    monkeypatch.setattr(bedrock._router_service, "resolve_bedrock_model", _resolve)
    monkeypatch.setattr(bedrock._router_service, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(bedrock._router_service, "check_client_model_scope", lambda *a, **k: None)
    monkeypatch.setattr(rate_limit_enforcement, "enforce_rate_limits", _no_limit)

    class _Request:
        scope = {"state": {"auth_context": auth_context_vk, "_redis": mock_redis,
                           "request_id": "req-pt"}}
        app = SimpleNamespace(state=SimpleNamespace(
            provider_registry=SimpleNamespace(get=lambda _p: _Adapter()),
            cost_recorder=CostRecorder(),
            metrics=None,
        ))

        async def body(self) -> bytes:
            return b'{"max_tokens": 16}'

    resp = await bedrock._handle_bedrock(
        _Request(), model.alias, "invoke-with-response-stream", stream=True
    )

    async def consume():
        async for _ in resp.body_iterator:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(in_xadd.wait(), timeout=2.0)
    task.cancel()  # = 종결 청크를 읽자마자 닫는 클라이언트(StreamingResponse 의 http.disconnect)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written == [], "XADD 가 끊김보다 먼저 끝났다 — 이 테스트가 창을 못 맞췄다"
    release.set()
    await asyncio.wait_for(_until_written(written), timeout=2.0)

    assert [account_id_of(a.role_arn) for a in signed_with] == ["111122223333"]
    assert (written[0]["request_id"], written[0]["aws_account_id"]) == ("req-pt", "111122223333")
    assert (written[0]["input_tokens"], written[0]["output_tokens"]) == (5, 7)


async def _until_written(written: list) -> None:
    import asyncio

    while not written:
        await asyncio.sleep(0.005)
