# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: chat SSE 릴레이의 세 결함 — 에러가 사용자에게 **한 번도** 안 보이던 문제.

1. **에러 프레임이 조용히 폐기됐다.** 릴레이는 에러 본문을
   ``{"error": …, "type": "<예외클래스명>"}`` 로 실어보냈다. admin-ui 의
   ``parseSseBlock`` 은 프레임을 ``{type: <event 이름>, ...payload}`` 로 합치므로
   본문의 ``type`` 이 이벤트 이름 ``'error'`` 를 덮어썼다(→ ``type: 'ClientError'``).
   ChatLayout 의 ``case 'error'`` 는 절대 매칭되지 않고 ``default: return msg`` 로
   버려져 — AgentCore 호출이 실패해도 화면엔 아무 메시지 없이 pending 스피너만
   영구히 돌았다. (다른 이벤트는 본문에 ``type`` 이 없어 **에러 프레임만** 충돌했다.)

2. **절단된 스트림을 성공으로 보고했다.** ``iter_lines`` 가 None 을 돌려주면
   (런타임 종료/네트워크 절단/read timeout) 그대로 ``event: done`` 만 발행했다.
   agent 는 정상 완료 시 반드시 마지막에 ``{"type":"done"}`` 을 yield 하므로
   (admin-chat-agent/src/agent/main.py:1503) 그 부재가 절단의 신호다 — 예전엔
   잘린 분석이 **완결된 답변으로** 표시됐다.

3. **예외 경로에서 assistant 턴이 통째로 사라졌다.** 영속화가 ``try`` 안,
   스트림 루프 뒤에만 있어 예외가 나면 이미 흘려보낸 텍스트·도구호출·차트가
   DB 에 하나도 남지 않았다(새로고침하면 질문만 남는다).

부수적으로 같은 경로의 **프레임 쪼개짐** 위험도 닫았다: ``event:`` 줄과 ``data:``
줄을 따로 publish 하면 그 사이에 ``_StreamRelay.tail`` 의 keepalive 코멘트가 끼어
블록이 갈라질 수 있다 → 이제 ``_sse()`` 로 한 프레임에 담는다.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.routers import chat_agent as m

SOURCE_PATH = Path(m.__file__)


# ──────────────────────────────────────────────────────────────────────────────
# 하네스 — StreamingBody / boto3 client / DB 세션 대역
# ──────────────────────────────────────────────────────────────────────────────


class _FakeBody:
    """botocore StreamingBody 대역. SSE 라인을 하나씩 흘린다."""

    def __init__(self, lines: list[bytes], raise_at: int | None = None) -> None:
        self._lines = list(lines)
        self._raise_at = raise_at

    def iter_lines(self, chunk_size: int = 1024):  # noqa: ARG002 - 시그니처 호환
        for i, line in enumerate(self._lines):
            if self._raise_at is not None and i == self._raise_at:
                raise ConnectionResetError("peer가 스트림 중간에 끊었다")
            yield line


class _FakeClient:
    def __init__(self, body: _FakeBody | None = None, exc: Exception | None = None) -> None:
        self._body = body
        self._exc = exc

    def invoke_agent_runtime(self, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return {"response": self._body}


class _FakeSession:
    def __init__(self, store: list, fail: bool = False) -> None:
        self._store = store
        self._fail = fail

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc_info) -> bool:
        return False

    async def execute(self, stmt):
        if self._fail:
            raise RuntimeError("DB down")
        self._store.append(stmt)

    async def commit(self) -> None:
        return None


def _bound(stmt) -> dict:
    """``text(...).bindparams(...)`` 에서 실제 바인딩 값을 꺼낸다."""
    return {k: v.value for k, v in stmt._bindparams.items()}


async def _run(
    monkeypatch,
    *,
    lines: list[bytes] | None = None,
    invoke_exc: Exception | None = None,
    raise_at: int | None = None,
    db_fail: bool = False,
) -> tuple[m._StreamRelay, list]:
    """producer 를 한 번 돌리고 (relay, 영속화된 statement 목록) 을 돌려준다."""
    import boto3

    body = None if invoke_exc is not None else _FakeBody(lines or [], raise_at=raise_at)
    monkeypatch.setattr(
        boto3, "client", lambda *_a, **_k: _FakeClient(body=body, exc=invoke_exc)
    )
    inserted: list = []
    monkeypatch.setattr(
        m, "AsyncSessionLocal", lambda: _FakeSession(inserted, fail=db_fail)
    )

    relay = m._StreamRelay()
    await m._agentcore_producer(
        relay, "11111111-1111-1111-1111-111111111111", "질문"
    )
    return relay, inserted


def _frames(relay: m._StreamRelay) -> list[str]:
    return [f.decode() for f in relay.frames]


def _events(relay: m._StreamRelay) -> list[tuple[str, dict]]:
    """발행된 프레임을 (이벤트이름, 본문dict) 로 파싱. keepalive 코멘트는 제외."""
    out: list[tuple[str, dict]] = []
    for frame in _frames(relay):
        if frame.startswith(":"):
            continue
        name = ""
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data += line[len("data:") :].strip()
        out.append((name, json.loads(data) if data else {}))
    return out


def _frame(payload: dict) -> bytes:
    """AgentCore 가 흘리는 ``data: <json>`` 라인 한 줄.

    (bytes 리터럴은 non-ASCII 를 담을 수 없어 한글 페이로드는 반드시 encode 해야 한다.)
    """
    return f"data: {json.dumps(payload)}".encode()


def _text_frame(chunk: str) -> bytes:
    return _frame({"type": "text", "chunk": chunk})


DONE_FRAME = _frame({"type": "done", "session_id": "s", "phase": 4})


# ──────────────────────────────────────────────────────────────────────────────
# 0) 하네스 공허성 대조군
# ──────────────────────────────────────────────────────────────────────────────


async def test_the_fake_harness_actually_streams_and_persists(monkeypatch):
    """대조군 — 하네스가 아무것도 안 흘리면 아래 단정 전부가 공허해진다."""
    relay, inserted = await _run(
        monkeypatch, lines=[_text_frame("안녕"), b"", DONE_FRAME]
    )
    assert relay.frames, "프레임이 하나도 발행되지 않았다 — 하네스가 동작하지 않는다"
    assert len(inserted) == 1, f"영속화가 정확히 1회여야 한다: {len(inserted)}"
    assert relay.done is True, "finally 의 relay.finish() 가 호출되지 않았다"


# ──────────────────────────────────────────────────────────────────────────────
# 1) 결함 #1 — 에러 프레임 본문에 `type` 이 있으면 UI 가 통째로 버린다
# ──────────────────────────────────────────────────────────────────────────────

# 에러 프레임을 만드는 세 경로 전부.
ERROR_PATHS = {
    # (a) AgentCore in-band 에러 프레임
    "in_band": {
        "lines": [_frame({"error": "boom", "error_type": "ValidationException"})],
        "expected_type": "ValidationException",
    },
    # (b) done 없이 끊긴 스트림(절단)
    "truncated": {
        "lines": [_text_frame("부분 답변")],
        "expected_type": "StreamTruncated",
    },
    # (c) producer 예외
    "exception": {
        "invoke_exc": RuntimeError("자격증명 없음"),
        "expected_type": "RuntimeError",
    },
}


@pytest.mark.parametrize("path", sorted(ERROR_PATHS))
async def test_error_frame_body_never_carries_a_type_key(monkeypatch, path):
    """본문 ``type`` 은 SSE 이벤트 이름을 덮어써 ``case 'error'`` 를 무력화한다."""
    spec = ERROR_PATHS[path]
    relay, _ = await _run(
        monkeypatch,
        lines=spec.get("lines"),
        invoke_exc=spec.get("invoke_exc"),
    )
    errors = [(n, b) for n, b in _events(relay) if n == "error"]
    assert len(errors) == 1, f"{path}: error 프레임이 정확히 1개여야 한다: {errors}"
    _, body = errors[0]
    assert "type" not in body, (
        f"{path}: 에러 본문에 'type' 이 있다 — admin-ui parseSseBlock 이 이벤트 이름을 "
        f"덮어써 ChatLayout 의 case 'error' 가 잡지 못하고 조용히 버려진다: {body}"
    )
    assert body.get("error_type") == spec["expected_type"], (
        f"{path}: 예외 종류가 error_type 으로 전달돼야 한다: {body}"
    )
    assert body.get("error"), f"{path}: 사람이 읽을 메시지가 비었다: {body}"


def _merge_like_admin_ui(frame: str) -> dict:
    """admin-ui ``parseSseBlock`` 의 병합 규칙을 그대로 재현.

    규칙: ``event:`` 줄이 있으면 그게 payload 의 ``type`` 보다 **우선**한다.
    """
    name = ""
    data = ""
    for line in frame.split("\n"):
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()
    body = json.loads(data) if data else {}
    return {**body, "type": name or body.get("type") or "message"}


async def test_admin_ui_would_route_the_error_frame_to_the_error_branch(monkeypatch):
    """계약 시뮬레이션 — 병합 결과의 type 이 'error' 여야 UI 가 실패를 표시한다."""
    relay, _ = await _run(monkeypatch, lines=[], invoke_exc=RuntimeError("boom"))
    merged = [
        _merge_like_admin_ui(f) for f in _frames(relay) if f.startswith("event: error")
    ]
    assert merged, "error 프레임이 없다"
    assert merged[0]["type"] == "error", (
        f"UI 가 ChatLayout case 'error' 로 라우팅하지 못한다: {merged[0]}"
    )
    assert merged[0]["error"], "표시할 오류 메시지가 없다"


def test_the_merge_simulation_reproduces_the_old_defect():
    """대조군 — 위 시뮬레이션이 **예전 코드에서는 실패**해야 판별력이 있다.

    예전: 본문에 ``type`` 이 있고, UI 는 ``{type: event, ...payload}`` 로 합쳤다.
    """
    old_frame = 'event: error\ndata: {"error": "boom", "type": "ClientError"}\n\n'
    # 예전 UI 병합 순서(payload 가 나중 → 이벤트 이름을 덮어쓴다)
    name, data = "error", json.loads(old_frame.split("data: ", 1)[1].strip())
    old_merged = {"type": name, **data}
    assert old_merged["type"] == "ClientError", "예전 결함이 재현되지 않는다"
    assert old_merged["type"] != "error", (
        "예전 코드가 'error' 로 라우팅됐다면 이 결함은 존재하지 않았다"
    )
    # 지금 규칙(event: 우선)이면 같은 프레임도 올바르게 라우팅된다.
    assert _merge_like_admin_ui(old_frame)["type"] == "error"


# ──────────────────────────────────────────────────────────────────────────────
# 2) 결함 #2 — done 프레임 없이 끊긴 스트림은 성공이 아니다
# ──────────────────────────────────────────────────────────────────────────────


async def test_truncated_stream_is_reported_as_error(monkeypatch):
    relay, inserted = await _run(monkeypatch, lines=[_text_frame("절반만 온 답변")])
    names = [n for n, _ in _events(relay)]
    assert "error" in names, (
        f"done 프레임 없이 끝났는데 에러 보고가 없다 — 잘린 답변이 완결된 답변으로 "
        f"표시된다: {names}"
    )
    errors = [b for n, b in _events(relay) if n == "error"]
    assert errors[0]["error_type"] == "StreamTruncated"
    # 스피너 정리·[SUGGESTIONS] 추출을 위해 done 은 여전히 필요하다.
    assert "done" in names, f"클라이언트가 pending 을 정리할 done 이 없다: {names}"
    # 부분 답변은 남기고, 잘렸다는 사실을 본문에도 남긴다(복원 화면 = 라이브 화면).
    content = _bound(inserted[0])["c"]
    assert "절반만 온 답변" in content, f"부분 답변이 유실됐다: {content!r}"
    assert "[오류]" in content, f"복원 화면에 절단 사실이 남지 않는다: {content!r}"


async def test_complete_stream_is_not_flagged_as_truncated(monkeypatch):
    """대조군 — done 을 본 정상 스트림에는 에러가 붙지 않아야 한다."""
    relay, inserted = await _run(
        monkeypatch, lines=[_text_frame("완결된 답변"), b"", DONE_FRAME]
    )
    names = [n for n, _ in _events(relay)]
    assert "error" not in names, f"정상 완료인데 에러를 보고했다: {names}"
    assert "done" in names
    content = _bound(inserted[0])["c"]
    assert content == "완결된 답변"
    assert "[오류]" not in content


async def test_non_streaming_fallback_is_not_flagged_as_truncated(monkeypatch):
    """비스트리밍 응답엔 done 프레임 자체가 없다 — 절단으로 오판하면 안 된다."""
    import boto3

    class _NonStreamBody:
        def read(self):
            return json.dumps({"reply": "단일 응답"}).encode()

    monkeypatch.setattr(
        boto3, "client", lambda *_a, **_k: _FakeClient(body=_NonStreamBody())
    )
    inserted: list = []
    monkeypatch.setattr(m, "AsyncSessionLocal", lambda: _FakeSession(inserted))
    relay = m._StreamRelay()
    await m._agentcore_producer(relay, "22222222-2222-2222-2222-222222222222", "질문")

    names = [n for n, _ in _events(relay)]
    assert "error" not in names, f"fallback 을 절단으로 오판했다: {names}"
    assert "done" in names


# ──────────────────────────────────────────────────────────────────────────────
# 3) 결함 #3 — 예외 경로에서도 부분 답변을 영속화한다
# ──────────────────────────────────────────────────────────────────────────────


async def test_exception_before_the_stream_still_persists_the_turn(monkeypatch):
    """invoke 자체가 실패하는 경로.

    누적 변수들이 ``try`` 안에 선언돼 있으면 이 경로에서 except 핸들러가 NameError 로
    다시 죽는다 — 그래서 선언이 try 밖에 있어야 한다.
    """
    relay, inserted = await _run(monkeypatch, invoke_exc=RuntimeError("자격증명 없음"))
    assert len(inserted) == 1, "예외 경로에서 assistant 턴이 통째로 사라졌다"
    content = _bound(inserted[0])["c"]
    assert "RuntimeError" in content and "자격증명 없음" in content, (
        f"실패 사실이 본문에 남지 않았다: {content!r}"
    )
    assert [n for n, _ in _events(relay) if n == "error"], "에러 보고도 없다"


async def test_exception_midstream_keeps_the_partial_answer(monkeypatch):
    """텍스트를 흘리던 중 연결이 끊긴 경로 — 이미 보여준 답변은 남아야 한다."""
    relay, inserted = await _run(
        monkeypatch,
        lines=[_text_frame("여기까지는 사용자가 봤다"), _text_frame("이건 못 봤다")],
        raise_at=1,
    )
    assert len(inserted) == 1
    content = _bound(inserted[0])["c"]
    assert "여기까지는 사용자가 봤다" in content, (
        f"화면에 이미 흘러간 부분 답변이 DB 에 없다: {content!r}"
    )
    assert "ConnectionResetError" in content


async def test_running_tool_calls_are_marked_failed_on_the_error_path(monkeypatch):
    """실패 종료면 미완 도구는 failed — done 으로 저장하면 복원 화면이 성공처럼 보인다."""
    relay, inserted = await _run(
        monkeypatch,
        lines=[
            _frame({"type": "tool_call", "tool": "sql_specialist", "args": {}}),
            _text_frame("부분"),
        ],
        raise_at=2,
    )
    tool_calls = json.loads(_bound(inserted[0])["tc"])
    assert tool_calls[0]["status"] == "failed", (
        f"실패 종료인데 도구가 {tool_calls[0]['status']} 로 저장됐다"
    )


async def test_persist_failure_does_not_swallow_the_error_report(monkeypatch):
    """영속화가 또 실패해도 사용자에겐 실패가 보고돼야 한다."""
    relay, inserted = await _run(
        monkeypatch, invoke_exc=RuntimeError("boom"), db_fail=True
    )
    assert not inserted
    assert [n for n, _ in _events(relay) if n == "error"], (
        "DB 실패가 에러 보고를 삼켰다"
    )
    assert relay.done is True, "relay.finish() 까지 도달하지 못했다"


async def test_the_turn_is_persisted_exactly_once(monkeypatch):
    """절단 경로는 _persist 를 부르고 그 뒤 예외가 나도 중복 INSERT 하지 않는다."""
    relay, inserted = await _run(monkeypatch, lines=[_text_frame("한 번만")])
    assert len(inserted) == 1, f"중복 영속화: {len(inserted)}"


# ──────────────────────────────────────────────────────────────────────────────
# 4) 프레임 원자성 — event:/data: 가 쪼개지면 이벤트가 통째로 사라진다
# ──────────────────────────────────────────────────────────────────────────────


async def test_every_published_frame_is_a_complete_sse_block(monkeypatch):
    """``event: X\\n`` 만 든 프레임이 있으면 keepalive 가 그 뒤에 끼어들 수 있다."""
    relay, _ = await _run(
        monkeypatch,
        lines=[
            _frame({"type": "thinking", "text": "생각중"}),
            _frame(
                {"type": "heartbeat", "phase": "sql", "label": "L", "elapsed_ms": 1}
            ),
            _frame({"type": "chart", "spec": {"kind": "bar"}}),
            _text_frame("본문"),
            DONE_FRAME,
        ],
    )
    assert len(relay.frames) >= 5, "프레임이 부족하다 — 하네스 확인"
    for frame in _frames(relay):
        if frame.startswith(":"):
            continue  # keepalive 코멘트
        assert "data:" in frame, (
            f"event: 줄만 있는 프레임 — tail() 의 keepalive 가 끼면 블록이 갈라져 "
            f"이벤트가 사라진다: {frame!r}"
        )
        assert frame.endswith("\n\n"), f"블록 종결(\\n\\n)이 없다: {frame!r}"


def test_all_error_frames_go_through_the_single_helper():
    """error 프레임을 만드는 곳은 ``_publish_error`` **하나**여야 한다.

    본문 키 규칙(``type`` 이 아니라 ``error_type``)이 한 곳에만 있어야 유지된다 —
    새 발행 지점이 직접 ``_sse("error", {...})`` 를 부르면 규칙이 갈라진다.
    """
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))

    total_sse_calls = 0
    error_sse_owners: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_sse":
                continue
            total_sse_calls += 1
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and first.value == "error":
                error_sse_owners.append(func.name)

    # 대조군 — 파서가 실제로 발행 지점을 찾았는가(이름이 바뀌면 위 전체가 공허해진다).
    assert total_sse_calls >= 10, (
        f"_sse 호출을 {total_sse_calls}개만 찾았다 — 헬퍼 이름이 바뀌었거나 파싱이 "
        f"빗나갔다(중첩 함수까지 세므로 실제보다 클 수 있다)"
    )
    assert error_sse_owners == ["_publish_error"], (
        f"error 프레임을 만드는 함수가 {error_sse_owners} 다 — _publish_error 만이어야 "
        f"한다(본문 키를 type 으로 되돌린 지점이 생기면 UI 가 다시 조용히 버린다)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4) done 프레임은 durationMs 를 싣고, 영속화는 같은 값을 duration_ms 로 남긴다
# ──────────────────────────────────────────────────────────────────────────────
#
# done 프레임은 오랫동안 리터럴 `{}` 였고 INSERT 는 duration_ms 컬럼을 쓰지 않았다. admin-ui 의
# MessageBubble footer 는 `durationMs`(done 프레임, camelCase — useChatStream 이 JSON 을 그대로
# spread 한다)와 `duration_ms`(/history, 매퍼가 변환)를 읽는데, 둘 다 영구히 비어 footer 가 아무
# 오류 없이 사라져 있었다. 실 PG 의 컬럼 왕복은 test_high_console_contract_real_db.py 가 증명한다.


async def test_done_frame_carries_duration_ms_in_camel_case(monkeypatch):
    relay, inserted = await _run(monkeypatch, lines=[_text_frame("답"), DONE_FRAME])

    done = [body for name, body in _events(relay) if name == "done"]
    assert len(done) == 1
    assert isinstance(done[0].get("durationMs"), int) and done[0]["durationMs"] >= 0, done[0]
    assert "duration_ms" not in done[0], "snake_case 는 클라이언트가 spread 해도 못 읽는다"
    # costUsd 는 근거(토큰 사용량)가 없으므로 싣지 않는다 — 0 은 '무료' 라는 틀린 숫자다.
    assert "costUsd" not in done[0]

    assert len(inserted) == 1
    assert _bound(inserted[0])["dur"] == done[0]["durationMs"], (
        "라이브 footer 와 새로고침 후 footer 가 다른 숫자를 보인다"
    )


async def test_failed_stream_still_persists_a_duration(monkeypatch):
    relay, inserted = await _run(monkeypatch, invoke_exc=RuntimeError("agentcore down"))
    assert len(inserted) == 1
    dur = _bound(inserted[0])["dur"]
    assert isinstance(dur, int) and dur >= 0
