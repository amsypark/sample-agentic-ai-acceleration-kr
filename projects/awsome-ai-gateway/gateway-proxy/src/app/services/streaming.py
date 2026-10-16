# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from starlette.requests import Request

from app.config import get_settings
from app.providers.openai_usage import extract_chat_usage, extract_responses_usage
from app.schemas.domain import TokenUsage
from app.services import bg_tasks
from app.services.account_credentials import redact_account_identifiers

logger = structlog.get_logger(__name__)

# 2번째 인자 = 첫 콘텐츠 델타의 time.monotonic() (미검출 시 None)
OnUsage = Callable[[TokenUsage, float | None], Awaitable[None]] | None
# KI-08 tokenizer fallback: 누적된 output 텍스트를 받아 토큰 수를 역산.
# None 반환 시 추정 불가 (cost_recorder는 0 토큰 + estimated=True로 기록).
TokenizerHook = Callable[[str], Awaitable[int | None]] | None

#: 잘린 스트림의 프롬프트 usage 추정 — 인자 없음, ``TokenUsage``(입력 버킷만) 또는 None.
#: Responses / chat 방언은 usage 를 종결 이벤트에만 실어서, 그 전에 끊기면 입력이 0 으로 남는다.
#: 라우터가 요청 body 로 만들어 넘긴다(tokenizer.estimate_prompt_usage). 비-2xx 스트림에는 넘기지
#: 않는다 — 상류가 받지 않은 요청의 프롬프트를 청구하면 안 된다.
PromptUsageHook = Callable[[], Awaitable[TokenUsage | None]] | None


async def _with_prompt_estimate(usage: TokenUsage, hook: PromptUsageHook) -> TokenUsage:
    """usage 이벤트 없이 끊긴 스트림의 입력 0 을 추정으로 채운다(KI-08 의 입력 쪽).

    호출자는 "usage 이벤트를 하나도 못 받았고, 상류가 요청을 받아들인 이벤트를 하나라도 보냈다"
    일 때만 부른다 — 출력 텍스트가 없어도(추론 중이거나 도구 호출만) 프롬프트는 이미 처리됐다.
    오류 프레임만 온 스트림은 받아들여지지 않은 것이라 부르지 않는다. 실패하면 원래 usage
    그대로(크래시 없음).
    """
    if hook is None:
        return usage
    try:
        est = await hook()
    except Exception:
        logger.warning("prompt_usage_hook_failed")
        return usage
    if est is None:
        return usage
    prompt = est.input_tokens + est.cache_read_input_tokens + est.cache_creation_input_tokens
    if prompt <= 0:
        return usage
    return usage.model_copy(
        update={
            "input_tokens": est.input_tokens,
            "cache_read_input_tokens": est.cache_read_input_tokens,
            "cache_creation_input_tokens": est.cache_creation_input_tokens,
            "total_tokens": prompt + usage.output_tokens,
            "estimated": True,
        }
    )


#: SSE 프레임 경계(빈 줄). strip_usage_chunk 모드가 네트워크 청크를 프레임 단위로 다시 묶는다.
_SSE_FRAME_END = re.compile(rb"\r?\n\r?\n")


def _is_usage_only_frame(frame: bytes) -> bool:
    """``data: {..., "choices": [], "usage": {...}}`` — include_usage 가 붙이는 마지막 청크."""
    line = frame.strip()
    if not line.startswith(b"data:"):
        return False
    try:
        data = json.loads(line[len(b"data:"):].strip())
    except (ValueError, TypeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("choices") == []
        and isinstance(data.get("usage"), dict)
    )


#: 스트림 종료 훅 — ``(누적된 원본 SSE 프레임 전문, status)``.
#: status 는 ``"success"`` 또는 ``"partial"``(disconnect / timeout / 중간 예외).
#:
#: ⚠️ 이 훅이 설정되면 제너레이터가 **모든 프레임을 메모리에 누적한다.** 동시 스트림 수 ×
#:    응답 크기만큼 메모리를 쓴다. 그래서 훅이 None 이면 한 바이트도 쌓지 않는다 —
#:    본문 로깅이 꺼진 상태의 비용이 0 이어야 한다.
OnComplete = Callable[[str, str], Awaitable[None]] | None


def _resolve_timeouts(
    idle_timeout: float | None, drain_timeout: float | None
) -> tuple[float, float]:
    """SSE 타임아웃의 **단일 진실원**: 인자 미지정(None) 시 Settings 에서 해석.

    과거엔 각 헬퍼의 기본값이 하드코딩 60.0/30.0 이었고 **모든 호출부가 인자를 넘기지
    않아** `settings.stream_idle_timeout` / `stream_disconnect_drain_timeout` 이 완전한
    **죽은 설정**이었다 — 차트/env 로 어떤 값을 주입해도 런타임은 60s 로 동작했고,
    그래서 Opus extended thinking 요청이 정상 생성 중에 끊겼다.

    호출부는 5곳(routers/messages.py, routers/openai_compat.py×2,
    services/web_search_loop.py×3 — pass-through 경로 포함)이라 "호출부마다 인자 추가"
    방식은 새 호출부가 하나 생기는 순간 같은 회귀가 재발한다. 그래서 기본값 자체를
    설정에서 끌어오게 만들어 구조적으로 막는다.

    인자를 명시하면 그대로 우선한다(단위테스트가 idle_timeout=0.2 로 타임아웃 경로를
    강제하는 것처럼). 둘 다 명시된 경우엔 get_settings() 를 아예 호출하지 않는다.
    """
    if idle_timeout is not None and drain_timeout is not None:
        return idle_timeout, drain_timeout
    s = get_settings()
    return (
        float(s.stream_idle_timeout) if idle_timeout is None else idle_timeout,
        float(s.stream_disconnect_drain_timeout) if drain_timeout is None else drain_timeout,
    )


async def bedrock_anthropic_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
) -> AsyncIterator[bytes]:
    """Bedrock EventStream chunks → Anthropic SSE-formatted bytes.

    Each upstream chunk is a JSON blob (one Anthropic event); we emit
    `event: <type>\\ndata: <json>\\n\\n` to the client. Token usage is
    aggregated across `message_start` (input + cache) and `message_delta`
    (output), with `amazon-bedrock-invocationMetrics` as a fallback.

    Edge case handling:
    - Client disconnect: stop yielding; drain remaining chunks in a
      background task so usage is still recorded (best-effort).
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit
      `event: error` SSE and return. Prevents hung upstream streams from
      pinning the connection.
    - Upstream exception mid-stream: emit `event: error` SSE with the
      error message and return gracefully (do not propagate).
    - Malformed JSON chunk: passthrough as `data: <raw>\\n\\n` (no crash).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    # 떼어 낸 회계·drain 태스크 이름에 붙는다 — 셧다운에 남으면 이 id 로 원장을 대사한다.
    request_id = bg_tasks.request_id_of(request)
    counters = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }
    accumulated_text: list[str] = []  # KI-08: content_block_delta.delta.text 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    # 과금 멱등 가드. 종료 경로가 4개(정상/idle timeout/upstream 예외/클라이언트 끊김
    # → 백그라운드 drain)라, 가드 없이 각 경로에서 finalize 를 호출하면 **이중 과금**이
    # 된다. 반대로 timeout/예외 경로에서 호출을 빼면 그때까지 생성된 토큰이 **유실**된다
    # (과거 동작: 두 경로가 함수 말미의 usage 발화를 건너뛰는 return 이었다).
    usage_fired = False
    # 본문 로깅용 원본 SSE 프레임 누적. on_complete 가 없으면 아무것도 쌓지 않는다.
    accumulated_frames: list[str] = []
    complete_fired = False

    def _format(chunk: bytes) -> bytes:
        """Parse chunk, update counters, return SSE-formatted bytes."""
        nonlocal first_token_time
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            raw = (
                chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            )
            return f"data: {raw}\n\n".encode()

        etype = data.get("type", "unknown")
        if etype == "message_start":
            u = data.get("message", {}).get("usage", {})
            if v := u.get("input_tokens"):
                counters["input_tokens"] = v
            counters["cache_creation"] = u.get(
                "cache_creation_input_tokens", counters["cache_creation"]
            )
            counters["cache_read"] = u.get("cache_read_input_tokens", counters["cache_read"])
        elif etype == "content_block_delta":
            # KI-08: 스트림 도중 생성된 텍스트 누적. disconnect 시 tokenizer 역산용.
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                if t := delta.get("text"):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(t)
        elif etype == "message_delta":
            u = data.get("usage", {})
            if v := u.get("output_tokens"):
                counters["output_tokens"] = v
        elif m := data.get("amazon-bedrock-invocationMetrics"):
            # invocationMetrics는 Bedrock이 최종 집계한 billable 토큰 수.
            # message_delta.usage.output_tokens와 미세하게 다를 수 있으므로
            # (extended thinking 모델에서 ~3 토큰 차이) 항상 이 값으로 덮어쓴다.
            if v := m.get("inputTokenCount"):
                counters["input_tokens"] = v
            if v := m.get("outputTokenCount"):
                counters["output_tokens"] = v

        return f"event: {etype}\ndata: {json.dumps(data)}\n\n".encode()

    def _current_usage() -> TokenUsage | None:
        it, ot = counters["input_tokens"], counters["output_tokens"]
        if not (it or ot):
            return None
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=it + ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
        )

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: output_tokens=0 인데 누적 텍스트가 있으면 tokenizer로 역산.

        disconnect 케이스: message_start로 input_tokens는 있으나 message_delta 전
        끊김 → output_tokens=0. 누적된 content_block_delta text로 역산.
        """
        if not tokenizer_hook or not accumulated_text:
            return usage
        it = counters["input_tokens"]
        ot = counters["output_tokens"]
        if ot > 0:
            return usage  # provider에서 실제 usage 이벤트 수신됨 → 역산 불필요
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        return TokenUsage(
            input_tokens=it,
            output_tokens=estimated_ot,
            total_tokens=it + estimated_ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
            estimated=True,
        )

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    def _claim_usage() -> bool:
        """과금 슬롯을 **정확히 1회** 선점한다(usage_fired 가드) — 위 4개 종료 경로 중
        어디로 빠져도 과금이 유실되지도, 중복되지도 않게.

        ⚠️ 동기여야 한다. ``_spawn_finalize`` 는 회계 태스크를 띄운 뒤 루프에 양보하는데,
           timeout 경로에서는 그 창에 도착한 끊김이 취소 핸들러의 drain 을 돌린다. 클레임이
           태스크 안에 있으면 drain 이 아직 열린 가드를 보고 같은 요청을 한 번 더 청구한다.
        """
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return False
        usage_fired = True
        return True

    async def _run_on_usage() -> None:
        """KI-08: usage 미추출 시에도 빈 TokenUsage로 콜백 실행.

        라우터가 TPM 예약을 설정한 경우, 빈 usage로도 콜백이 돌아가야
        cost_recorder가 ``settle_tpm(actual=0)``을 호출해 예약 해제함.
        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.

        호출자가 이미 클레임(``_claim_usage``)을 쥐고 있어야 한다. 역산이 클레임 **뒤**, 이
        코루틴 안에 있는 것은 의도다 — 가드가 닫힌 뒤 역산 중에 잘리면 그 요청은 원장도 차감도
        없이 사라지므로, 역산까지 보호 태스크에 실린다.
        """
        base = _current_usage()
        estimated = await _estimate_if_needed(base)
        usage = estimated or base or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _finalize(complete_status: str, *, fire_usage: bool) -> None:
        """과금과 본문 로그를 **한 단위**로 — 한 태스크에 실어야 둘 다 살아남는다.

        ⚠️ 과금만 보호하면 본문 로그에 도달하지 못한다. ``shield`` 는 호출자 쪽에
           ``CancelledError`` 를 다시 던지므로 그 다음 줄은 실행되지 않는다.
        """
        if fire_usage:
            await _run_on_usage()
        await _fire_on_complete(complete_status)

    async def _spawn_finalize(complete_status: str) -> None:
        """회계를 취소에서 분리된 태스크로 넘기고 기다린다(``services/bg_tasks.py``).

        모든 호출부가 끊김이 떨어질 수 있는 창 안에 있다 — 성공 경로는 마지막 yield 뒤(종결
        프레임을 읽자마자 닫는 클라이언트), timeout/예외 경로는 오류 프레임 yield 앞. 평범한
        await 면 끊김이 finalize 를 예산 차감과 원장 XADD 사이에서 조용히 자른다.
        """
        fire_usage = _claim_usage()
        await bg_tasks.spawn_shielded(
            _finalize(complete_status, fire_usage=fire_usage),
            name="bedrock_stream_finalize",
            request_id=request_id,
        )

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                _format(chunk)  # side effect: updates counters
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            # ⚠️ 회계는 여기서도 보호 태스크로 넘긴다. 이 drain 은 셧다운에서 한 번 취소된다
            #    (cancel_on_shutdown) — 그 취소가 이 finally 의 회계 도중에 떨어지면 차감과 XADD 가
            #    다시 갈라지고, 느린 토크나이저면 클레임만 소비된 채 행이 없다(리뷰 프로브로 실측).
            #    보호 태스크는 취소 대상이 아니라 셧다운 drain() 이 다시 스냅샷해 기다린다.
            await _spawn_finalize("partial")

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # ⚠️ yield 보다 **먼저** 확정한다. 클라이언트가 이미 끊긴 상태면 아래
                # yield 가 GeneratorExit 을 던져 except 절로 빠지므로, 뒤에 두면
                # 이 경로의 과금이 다시 유실된다.
                await _spawn_finalize("partial")
                err = {
                    "type": "error",
                    "error": {
                        "type": "timeout_error",
                        "message": f"upstream idle timeout after {idle_timeout}s",
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                return

            _frame = _format(chunk)
            if on_complete is not None:
                accumulated_frames.append(_frame.decode("utf-8", errors="replace"))
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. Spawn background
        # drain so usage is still recorded, then re-raise per asyncio contract.
        logger.info("bedrock_stream_cancelled")
        client_disconnected = True
        # bg_tasks.spawn — bare asyncio.create_task 는 약한 참조라 drain 이 도중에 GC 될 수
        # 있고, 셧다운 drain 대상에도 들지 않는다. cancel_on_shutdown: 셧다운 대기가 끝나도
        # 상류를 읽고 있으면 한 번 취소돼, finally 가 그때까지 읽은 usage 로 회계한다.
        bg_tasks.spawn(
            _drain_remaining(),
            name="bedrock_stream_drain",
            request_id=request_id,
            cancel_on_shutdown=True,
        )
        raise

    except Exception as exc:
        logger.exception("bedrock_stream_proxy_error")
        # timeout 경로와 동일 이유로 yield 앞에서 확정.
        await _spawn_finalize("partial")
        err = {
            "type": "error",
            # 상류 예외 문장에 서명 계정의 ARN 이 들어 있을 수 있다 — 원문은 위 로그에만.
            "error": {
                "type": "stream_error",
                "message": redact_account_identifiers(str(exc)) or "stream_error",
            },
        }
        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
        return

    if not client_disconnected:
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        # ⚠️ 마지막 yield 뒤다. 종결 프레임을 읽자마자 닫는 클라이언트의 끊김이 정확히 여기
        #    떨어진다 — 그래서 보호 태스크로 넘긴다.
        await _spawn_finalize("success")


async def openai_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
    prompt_usage_hook: PromptUsageHook = None,
    strip_usage_chunk: bool = False,
) -> AsyncIterator[bytes]:
    """OpenAI-compatible SSE chunks → passthrough bytes (no re-formatting).

    OpenAI upstreams already emit SSE-formatted frames (`data: {...}\\n\\n`,
    `data: [DONE]\\n\\n`). A single httpx chunk may contain one or many
    frames. We yield chunks as-is and scan each chunk for a `usage` object
    (vLLM emits it on the final chunk when `stream_options.include_usage`).

    ``strip_usage_chunk``: usage 를 게이트웨이가 강제로 켰는데(bedrock-runtime chat —
    bedrock_openai_adapter.with_chat_usage) 클라이언트는 요청하지 않았을 때. 청크를 완결된 프레임
    단위로 다시 묶어 usage 를 읽고, usage 전용 프레임(``choices: []``)만 클라이언트에서 뺀다.
    기본(False)은 지금까지와 바이트 단위로 같다(vLLM 경로).

    Edge case handling mirrors `bedrock_anthropic_sse_stream`:
    - Client disconnect: stop, background-drain so usage is still recorded.
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit an
      OpenAI-shaped error chunk (`data: {"error":{"type":"timeout_error",...}}\\n\\n`)
      and return.
    - Upstream exception mid-stream: same OpenAI-shaped error chunk path.
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    request_id = bg_tasks.request_id_of(request)  # 태스크 이름용(Bedrock 경로 참조)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []  # KI-08: delta.content 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False
    #: 상류가 생성 청크(choices)를 하나라도 보냈는지 — 요청을 받아들였다는 뜻(PromptUsageHook).
    upstream_started = False
    #: strip_usage_chunk 모드에서 아직 끝나지 않은 프레임 조각(네트워크 청크는 프레임 경계와 무관).
    pending_tail = b""

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"data: {json.dumps(payload)}\n\n".encode()

    def _scan_usage(chunk: bytes) -> TokenUsage | None:
        """Scan a (possibly multi-frame) chunk for usage + accumulate delta content."""
        nonlocal first_token_time, upstream_started
        try:
            text = chunk.decode("utf-8", errors="ignore")
        except Exception:
            return None
        found: TokenUsage | None = None
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "choices" in data:
                upstream_started = True  # 오류 청크({"error": ...})에는 choices 가 없다
            # KI-08: delta content 누적. OpenAI SSE: {"choices":[{"delta":{"content":"..."}}]}
            for choice in data.get("choices", []) or []:
                delta = choice.get("delta") or {}
                if isinstance(delta, dict) and (c := delta.get("content")):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(c)
            if u := data.get("usage"):
                # Shared Chat-wire parser: splits the cache-inclusive prompt count into
                # exclusive TokenUsage buckets and picks up reasoning_tokens. vLLM sends
                # none of the details sub-objects, so its behaviour is byte-identical to
                # the previous three-field construction; GPT-5.6 on either Bedrock plane
                # sends them and would otherwise be billed as if nothing were cached.
                found = extract_chat_usage(u)
        return found

    def _scan_chunk(chunk: bytes) -> bytes:
        """usage 를 읽고 클라이언트에 보낼 바이트를 돌려준다(strip_usage_chunk docstring)."""
        nonlocal latest_usage, pending_tail
        if not strip_usage_chunk or not isinstance(chunk, bytes):
            if u := _scan_usage(chunk):
                latest_usage = u
            return chunk
        buf = pending_tail + chunk
        out: list[bytes] = []
        start = 0
        for m in _SSE_FRAME_END.finditer(buf):
            frame = buf[start:m.end()]
            start = m.end()
            if u := _scan_usage(frame):
                latest_usage = u
            if not _is_usage_only_frame(frame):
                out.append(frame)
        pending_tail = buf[start:]
        return b"".join(out)

    def _flush_tail() -> bytes:
        """끝에 남은(빈 줄로 닫히지 않은) 프레임 조각 — strip 모드에서만 생긴다."""
        nonlocal latest_usage, pending_tail
        tail, pending_tail = pending_tail, b""
        if not tail:
            return b""
        if u := _scan_usage(tail):
            latest_usage = u
        return b"" if _is_usage_only_frame(tail) else tail

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: usage 없고 누적 텍스트 있으면 tokenizer 역산."""
        if not tokenizer_hook or not accumulated_text:
            return usage
        if usage and usage.output_tokens > 0:
            return usage
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        # OpenAI path에서는 input_tokens가 없음 (usage 이벤트 없으면) — 0으로 둠.
        it = usage.input_tokens if usage else 0
        return TokenUsage(
            input_tokens=it,
            output_tokens=estimated_ot,
            total_tokens=it + estimated_ot,
            estimated=True,
        )

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    def _claim_usage() -> bool:
        """**정확히 1회** 선점(usage_fired 가드). 동기 — 이유는 Bedrock 경로의 같은 함수 참조."""
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return False
        usage_fired = True
        return True

    async def _run_on_usage() -> None:
        """KI-08: latest_usage 없어도 빈 TokenUsage로 콜백 실행 (TPM 예약 해제용).

        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.
        호출자가 이미 클레임을 쥐고 있어야 한다 — 역산까지 보호 태스크에 실린다.
        """
        estimated = await _estimate_if_needed(latest_usage)
        usage = estimated or latest_usage or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        if latest_usage is None and upstream_started:
            # usage 는 마지막 청크에만 온다 — 그 전에 끊기면 입력이 0 이다(PromptUsageHook).
            # 텍스트가 없어도(도구 호출만, 추론 중) 프롬프트는 이미 처리됐다.
            usage = await _with_prompt_estimate(usage, prompt_usage_hook)
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _finalize(complete_status: str, *, fire_usage: bool) -> None:
        """과금 + 본문 로그를 한 단위로 — Bedrock 경로의 같은 함수 참조."""
        if fire_usage:
            await _run_on_usage()
        await _fire_on_complete(complete_status)

    async def _spawn_finalize(complete_status: str) -> None:
        """회계를 취소에서 분리된 태스크로 — Bedrock 경로의 같은 함수 참조."""
        fire_usage = _claim_usage()
        await bg_tasks.spawn_shielded(
            _finalize(complete_status, fire_usage=fire_usage),
            name="openai_stream_finalize",
            request_id=request_id,
        )

    async def _drain_remaining() -> None:
        nonlocal latest_usage
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                _scan_chunk(chunk)  # usage 만 읽는다(클라이언트는 이미 없다)
            _flush_tail()
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            # ⚠️ 회계는 여기서도 보호 태스크로 넘긴다. 이 drain 은 셧다운에서 한 번 취소된다
            #    (cancel_on_shutdown) — 그 취소가 이 finally 의 회계 도중에 떨어지면 차감과 XADD 가
            #    다시 갈라지고, 느린 토크나이저면 클레임만 소비된 채 행이 없다(리뷰 프로브로 실측).
            #    보호 태스크는 취소 대상이 아니라 셧다운 drain() 이 다시 스냅샷해 기다린다.
            await _spawn_finalize("partial")

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                if tail := _flush_tail():  # try 안에서 — 끊김 핸들러가 이 yield 도 덮는다
                    yield tail
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                await _spawn_finalize("partial")
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            chunk = _scan_chunk(chunk)
            if not chunk:
                continue  # usage 전용 프레임만 있었다(strip 모드)
            if on_complete is not None:
                accumulated_frames.append(
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, bytes)
                    else str(chunk)
                )
            yield chunk

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. See the twin
        # handler in `bedrock_anthropic_sse_stream` for rationale.
        logger.info("openai_stream_cancelled")
        client_disconnected = True
        bg_tasks.spawn(  # 강한 참조 + 셧다운 취소 대상 — Bedrock 경로의 같은 핸들러 참조
            _drain_remaining(),
            name="openai_stream_drain",
            request_id=request_id,
            cancel_on_shutdown=True,
        )
        raise

    except Exception as exc:
        logger.exception("openai_stream_proxy_error")
        await _spawn_finalize("partial")  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        # 상류 예외 문장에 서명 계정의 ARN 이 들어 있을 수 있다 — 원문은 위 로그에만.
        yield _emit_error_chunk(
            "stream_error", redact_account_identifiers(str(exc)) or "stream_error"
        )
        return

    if not client_disconnected:
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        # ⚠️ 마지막 yield 뒤 — 보호 태스크로 넘기는 이유는 Bedrock 경로의 같은 꼬리 참조.
        await _spawn_finalize("success")


async def responses_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    on_complete: OnComplete = None,
    tokenizer_hook: TokenizerHook = None,
    prompt_usage_hook: PromptUsageHook = None,
) -> AsyncIterator[bytes]:
    """OpenAI **Responses API** → re-framed SSE (`event: {type}\\ndata: {json}\\n\\n`).

    The MantleOpenAIAdapter yields RAW JSON event payloads (one per chunk, `data:`
    prefix already stripped — same contract as MantleAdapter/bedrock_anthropic_sse_stream).
    This stream parses each raw JSON chunk, accumulates text/usage, and re-frames it as
    a proper SSE event for the client (mirrors bedrock_anthropic_sse_stream, NOT the
    openai_sse_stream passthrough which assumes upstream is already SSE-framed).

    Usage shape: the terminal `response.completed` event carries final usage nested at
    `event["response"]["usage"]` (input/output/total + output_tokens_details.reasoning_tokens).
    `response.incomplete`/`response.failed` may carry usage or null. Text deltas arrive
    as `response.output_text.delta`. reasoning_tokens is a submetric (already inside
    output_tokens) — never re-added to total/cost.

    idle/drain 타임아웃 기본값은 Settings 에서 해석된다(_resolve_timeouts).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    request_id = bg_tasks.request_id_of(request)  # 태스크 이름용(Bedrock 경로 참조)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False
    #: 상류가 요청을 받아들인 이벤트(response.created, 추론·도구 델타 등)를 보냈는지 —
    #: PromptUsageHook 의 조건. 오류 이벤트는 세지 않는다.
    upstream_started = False

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()

    def _usage_from_response(resp: dict) -> TokenUsage | None:
        # Same parser as the non-streaming adapters (providers/openai_usage) — that is
        # what guarantees a prompt bills identically streamed vs non-streamed, on both the
        # Mantle and the bedrock-runtime plane. None (not a zero TokenUsage) when the
        # event carries no usage object, so a usage-less `response.incomplete` cannot
        # erase a good reading from an earlier terminal event.
        if not isinstance(resp.get("usage"), dict):
            return None
        return extract_responses_usage(resp)

    def _process(chunk: bytes) -> bytes:
        """Parse a RAW JSON event chunk → update usage/text, return re-framed SSE bytes.

        Malformed JSON is passed through as a bare `data:` frame (no crash), matching
        bedrock_anthropic_sse_stream's defensive behaviour.
        """
        nonlocal latest_usage, first_token_time, upstream_started
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = chunk.decode("utf-8", errors="ignore").strip()
            return f"data: {raw}\n\n".encode()
        if isinstance(data, dict):
            etype = data.get("type", "")
            if etype and etype not in ("error", "response.failed"):
                upstream_started = True
            if etype == "response.output_text.delta":
                if (d := data.get("delta")) and isinstance(d, str):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(d)
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                resp = data.get("response")
                if isinstance(resp, dict) and (u := _usage_from_response(resp)):
                    latest_usage = u
            etype_label = etype or "message"
            return f"event: {etype_label}\ndata: {json.dumps(data)}\n\n".encode()
        # Non-dict JSON (unexpected) — re-frame defensively.
        return f"data: {json.dumps(data)}\n\n".encode()

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _estimate_output_tokens() -> TokenUsage | None:
        """KI-08 역산 — 이 방언에만 없던 것.

        ⚠️ 왜 필요한가. Responses 방언은 usage 를 **종결 이벤트(response.completed)
           안에서만** 준다. 그래서 상류가 idle timeout 이나 오류로 그 이벤트 전에 끊기면
           ``latest_usage`` 가 None 이고, ``TokenUsage()``(전부 0)로 발화한다.
           ``cost_recorder.finalize`` 는 total/input/output 이 모두 0 이면 TPM 예약만
           돌려주고 **usage_logs 행을 아예 만들지 않은 채** 리턴한다 — provider 가 이미
           AWS 에 청구한 토큰이 우리 쪽에는 존재하지 않게 된다.

           같은 요청 형태가 ``/v1/messages`` 와 ``/v1/chat/completions`` 에서는 역산되어
           기록된다. 즉 이 누락은 **방언별**이라 집계 대시보드에서는 보이지 않는다.

        ⚠️ input 과 cache 버킷을 **앞으로 이어 나른다.** response.incomplete/failed 는
           input+cache 를 담고 output 만 0 인 경우가 있어서, output 만으로 TokenUsage 를
           새로 만들면 캐시 버킷이 지워져 지금보다 더 과소청구가 된다.
        """
        if not tokenizer_hook or not accumulated_text:
            return None
        base = latest_usage or TokenUsage()
        if base.output_tokens > 0:
            return None  # 실제 usage 를 받았다 — 역산 불필요
        try:
            estimated = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            return None
        if not estimated or estimated <= 0:
            return None
        # ⚠️ 새로 만들지 않고 **복사 후 덮어쓴다.** TokenUsage 에는 output 과 무관한 필드가
        #    더 있다(web_search_count — 웹서치 귀속/과금, cache_ttl_1h — 캐시 단가 분기).
        #    필드를 열거해 새로 만들면 나중에 필드가 추가될 때 조용히 유실된다.
        return base.model_copy(
            update={
                "output_tokens": estimated,
                "total_tokens": base.input_tokens + estimated,
                "estimated": True,
            }
        )

    def _claim_usage() -> bool:
        """**정확히 1회** 선점(usage_fired 가드). 동기 — 이유는 Bedrock 경로의 같은 함수 참조."""
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return False
        usage_fired = True
        return True

    async def _run_on_usage() -> None:
        """호출자가 이미 클레임을 쥐고 있어야 한다 — 역산까지 보호 태스크에 실린다."""
        usage = latest_usage or TokenUsage()
        estimated_usage = await _estimate_output_tokens()
        if estimated_usage is not None:
            usage = estimated_usage
        if latest_usage is None and upstream_started:
            # usage 는 종결 이벤트에만 온다 — 그 전에 끊기면 입력이 0 이다(PromptUsageHook).
            # 출력 텍스트가 없어도(추론 중, 도구 호출만) 프롬프트는 이미 처리됐다.
            usage = await _with_prompt_estimate(usage, prompt_usage_hook)
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _finalize(complete_status: str, *, fire_usage: bool) -> None:
        """과금 + 본문 로그를 한 단위로 — Bedrock 경로의 같은 함수 참조."""
        if fire_usage:
            await _run_on_usage()
        await _fire_on_complete(complete_status)

    async def _spawn_finalize(complete_status: str) -> None:
        """회계를 취소에서 분리된 태스크로 — Bedrock 경로의 같은 함수 참조.

        ⚠️ 유실이 측정된 바로 그 경로다. Codex CLI 는 ``response.completed`` 를 읽는 즉시
           스트림을 버리므로, 끊김이 매 턴 이 창에 떨어진다.
        """
        fire_usage = _claim_usage()
        await bg_tasks.spawn_shielded(
            _finalize(complete_status, fire_usage=fire_usage),
            name="responses_stream_finalize",
            request_id=request_id,
        )

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("responses_stream_drain_timeout")
                    break
                _process(chunk)  # updates latest_usage/accumulated_text as a side effect
        except Exception:
            logger.exception("responses_stream_drain_error")
        finally:
            await _spawn_finalize("partial")  # 보호 태스크로 — Bedrock 경로 drain 의 같은 주석

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("responses_stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                await _spawn_finalize("partial")
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            _frame = _process(chunk)
            if on_complete is not None:
                accumulated_frames.append(
                    _frame.decode("utf-8", errors="replace")
                    if isinstance(_frame, bytes)
                    else str(_frame)
                )
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        logger.info("responses_stream_cancelled")
        client_disconnected = True
        bg_tasks.spawn(  # 강한 참조 + 셧다운 취소 대상 — Bedrock 경로의 같은 핸들러 참조
            _drain_remaining(),
            name="responses_stream_drain",
            request_id=request_id,
            cancel_on_shutdown=True,
        )
        raise

    except Exception as exc:
        logger.exception("responses_stream_proxy_error")
        await _spawn_finalize("partial")  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        # 상류 예외 문장에 서명 계정의 ARN 이 들어 있을 수 있다 — 원문은 위 로그에만.
        yield _emit_error_chunk(
            "stream_error", redact_account_identifiers(str(exc)) or "stream_error"
        )
        return

    if not client_disconnected:
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        # ⚠️ 마지막 yield 뒤 — 보호 태스크로 넘기는 이유는 Bedrock 경로의 같은 꼬리 참조.
        await _spawn_finalize("success")


async def stream_response(
    request: Request,
    chunk_iterator: AsyncIterator[bytes],
    on_usage: callable,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
) -> AsyncIterator[bytes]:
    """스트리밍 응답 프록시.

    클라이언트에 chunk를 yield하며, 연결이 끊어지면 백그라운드에서
    스트림을 계속 소비하여 usage를 기록한다.

    ⚠️ 현재 **호출부 없음**(dialect 별 전용 헬퍼가 대체). 그래도 타임아웃 기본값을
    Settings 에서 해석하도록 맞춰 둔다 — 나중에 누가 이 함수를 쓰기 시작할 때
    하드코딩 60s 로 되돌아가는 회귀를 원천 차단하기 위함.
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    request_id = bg_tasks.request_id_of(request)  # 태스크 이름용(Bedrock 경로 참조)
    usage: TokenUsage | None = None
    client_disconnected = False

    async def _record_usage() -> None:
        try:
            await on_usage(usage, None)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def consume_remaining():
        """클라이언트 연결 끊김 후 백그라운드 소비."""
        nonlocal usage
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in chunk_iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                parsed_usage = _try_extract_usage(chunk)
                if parsed_usage:
                    usage = parsed_usage
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            if usage and callable(on_usage):
                # 보호 태스크로 — 세 제너레이터의 drain 과 같은 이유(셧다운 취소가 회계를 자른다).
                await bg_tasks.spawn_shielded(
                    _record_usage(), name="stream_response_finalize", request_id=request_id
                )

    try:
        async for chunk in chunk_iterator:
            # 클라이언트 연결 확인
            if await request.is_disconnected():
                logger.info("client_disconnected_during_stream")
                client_disconnected = True
                # 백그라운드에서 나머지 소비 (bg_tasks.spawn — 강한 참조 + 셧다운 drain 대상)
                bg_tasks.spawn(
                    consume_remaining(),
                    name="stream_response_drain",
                    request_id=request_id,
                    cancel_on_shutdown=True,
                )
                return

            parsed_usage = _try_extract_usage(chunk)
            if parsed_usage:
                usage = parsed_usage

            yield chunk

    except Exception:
        logger.exception("stream_proxy_error")
        client_disconnected = True

    if not client_disconnected and usage and callable(on_usage):
        # 위 세 제너레이터와 같은 이유로 보호한다(마지막 yield 뒤). 호출부 없는 함수지만,
        # 나중에 배선될 때 원장 누락이 함께 따라오지 않게 미리 막아 둔다.
        await bg_tasks.spawn_shielded(
            _record_usage(), name="stream_response_finalize", request_id=request_id
        )


def _try_extract_usage(chunk: bytes) -> TokenUsage | None:
    """청크에서 usage 추출 시도 (OpenAI SSE 형식)."""
    try:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                if usage := data.get("usage"):
                    return TokenUsage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        total_tokens=usage.get("total_tokens", 0),
                    )
    except Exception:
        pass
    return None
