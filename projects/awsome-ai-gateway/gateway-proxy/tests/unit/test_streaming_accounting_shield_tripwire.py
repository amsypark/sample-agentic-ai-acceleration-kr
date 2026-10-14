# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""구조 트립와이어: 스트리밍 성공 회계는 **반드시** 취소로부터 보호돼야 한다.

왜 행위 테스트만으로 부족한가. 이 결함의 성질은 "실패가 보이지 않는다" 다 — ``CancelledError``
가 ``BaseException`` 이라 ``except Exception`` 을 전부 통과하고, Redis 차감은 남고 원장 XADD 만
사라진다. 로그도 메트릭도 없다. 그래서 누군가 ``await bg_tasks.spawn_shielded(...)`` 를
"불필요한 간접" 으로 보고 평범한 ``await`` 로 되돌리면, 그 변경은 정상 경로의 동작을 전혀
바꾸지 않으므로 **대부분의 테스트가 통과하는 상태로** 결함이 복귀한다.

행위 테스트가 닫는 구멍과 이 파일이 닫는 구멍이 다르다:
  · ``tests/unit/test_stream_cancel_accounting.py`` — 세 제너레이터, ``/model/*`` 패스스루,
    웹서치 스티처와 본문 로그 래퍼의 실제 동작.
  · ``tests/regression/test_critical_stream_disconnect_ledger_gap.py`` — 실 uvicorn/소켓, 그리고
    실 Redis 위의 대사(카운터 == 원장).
  · 이 파일 — **모든 표면**의 배선과 셧다운 순서(행위로는 재현 비용이 큰 것까지).

트립와이어가 실패했다면 둘 중 하나다: (a) 보호가 제거됐다 — 복구할 것. (b) 설계가 바뀌었다 —
그렇다면 기대값을 **의도적으로** 갱신하고, 새 구조가 같은 성질을 보장하는 이유를 여기 적을 것.
"""

from __future__ import annotations

import ast
import pathlib
import re

# tests/unit/<this> → gateway-proxy/ → src/app
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "app"


def _read(rel: str) -> str:
    path = _SRC / rel
    assert path.exists(), f"경로가 바뀌었다: {path}"
    return path.read_text(encoding="utf-8")


def _functions(src: str) -> dict[str, list[ast.AST]]:
    """이름 → 함수 노드 목록. 중첩 함수까지 모은다(제너레이터 안의 헬퍼가 대상이다).

    같은 이름이 제너레이터마다 반복되므로(세 개의 ``_spawn_finalize``) 리스트로 쌓는다.
    """
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _calls(node: ast.AST) -> set[str]:
    """노드 하위의 모든 호출을 점 표기 문자열로."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                base = f.value
                prefix = base.id if isinstance(base, ast.Name) else "?"
                names.add(f"{prefix}.{f.attr}")
    return names


def _direct_awaited_names(fn: ast.AST) -> list[tuple[int, str]]:
    """``fn`` 의 **직접** 본문에서 ``await name(...)`` 만. 중첩 def 로는 내려가지 않는다.

    ★ 귀속은 가장 가까운 바깥 함수 기준이다. ``ast.walk`` 로 바깥 제너레이터에서 훑으면 중첩
      헬퍼의 await 까지 바깥 것으로 잡혀 전부 오탐이 된다.
    """
    found: list[tuple[int, str]] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            if n is not fn:
                return  # 중첩 함수는 따로 평가된다
            self.generic_visit(n)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Await(self, n):
            if isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name):
                found.append((n.lineno, n.value.func.id))
            self.generic_visit(n)

    V().visit(fn)
    return found


# ── 1. streaming.py — 세 제너레이터 + stream_response ─────────────────────────


def test_every_streaming_generator_has_a_shielded_finalize():
    """세 제너레이터 각각이 ``_spawn_finalize`` 를 정의하고, 그것이 shield 를 통과한다."""
    fns = _functions(_read("services/streaming.py"))
    spawns = fns.get("_spawn_finalize", [])
    # bedrock / openai / responses — 세 개.
    assert len(spawns) == 3, f"_spawn_finalize 가 {len(spawns)}개 (제너레이터 3개여야 한다)"
    for fn in spawns:
        calls = _calls(fn)
        assert "bg_tasks.spawn_shielded" in calls, (
            "_spawn_finalize 가 bg_tasks.spawn_shielded 를 쓰지 않는다 — 평범한 await 로 "
            "되돌아갔다면 클라이언트가 종결 프레임에서 끊는 순간 원장 XADD 가 잘린다."
        )
        # 클레임은 태스크를 띄우기 **전에**, 동기로(이중 청구 창 — _claim_usage docstring).
        assert "_claim_usage" in calls, "_spawn_finalize 가 동기 클레임을 먼저 잡지 않는다"


def test_streaming_exit_paths_do_not_bare_await_accounting():
    """제너레이터 **본문**(성공/timeout/예외 꼬리)이 회계를 직접 await 하면 안 된다.

    그 await 가 취소 지점이 되고, 그게 측정된 유실이다. 꼬리는 반드시 ``_spawn_finalize``
    (= shield)를 통과해야 한다. 반면 구현 헬퍼는 이미 떼어 낸 태스크 안에서 돌거나(drain,
    ``_finalize``) 그 태스크에 실려 가는 것이라 직접 await 가 맞다.
    """
    impl_helpers = {
        "_drain_remaining",
        "consume_remaining",
        "_fire_on_usage",
        "_fire_on_complete",
        "_run_on_usage",
        "_finalize",
        "_spawn_finalize",
        "_record_usage",
    }
    accounting = {"_fire_on_usage", "_fire_on_complete", "_run_on_usage", "on_usage", "on_complete"}
    offenders: list[str] = []
    for node in ast.walk(ast.parse(_read("services/streaming.py"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in impl_helpers:
            continue
        for lineno, callee in _direct_awaited_names(node):
            if callee in accounting:
                offenders.append(f"{node.name}:{lineno} -> {callee}")
    assert not offenders, (
        "회계를 보호 없이 await 하는 지점: "
        + ", ".join(offenders)
        + " — `_spawn_finalize` 를 쓸 것."
    )


def test_streaming_drains_use_strong_referenced_tasks():
    """``asyncio.create_task`` 로 띄운 drain 은 GC 로 사라질 수 있다.

    asyncio 는 실행 중 태스크를 **약한 참조**로만 든다. 핸들을 버린 bare task 는 도중에 사라질
    수 있고, 그 태스크가 하던 것이 바로 회계다. bg_tasks.spawn 은 강한 참조를 쥐고 셧다운 drain
    대상에도 넣는다.
    """
    src = _read("services/streaming.py")
    assert "asyncio.create_task(" not in src, (
        "streaming.py 에 asyncio.create_task 가 남아 있다 — bg_tasks.spawn 을 쓸 것."
    )
    assert src.count("bg_tasks.spawn(") >= 4, (
        "세 제너레이터 + stream_response 의 drain 배선이 빠졌다"
    )


# ── 2. web_search_loop.py — 두 스티처 ─────────────────────────────────────────


def _stitcher_finally_blocks(name: str) -> list[ast.Try]:
    fns = _functions(_read("services/web_search_loop.py"))
    (fn,) = fns.get(name, [None])
    assert fn is not None, f"{name} 가 사라졌다"
    return [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]


def test_both_web_search_stitchers_shield_their_usage():
    """두 스티처는 usage 를 ``finally`` 에서 쏜다 — 그 발화가 shield 를 통과해야 한다.

    이 ``finally`` 는 마지막 yield 뒤(종결 프레임 직후의 끊김)와 취소 **그 자체** 때문에 돈다.
    anyio 취소는 level-triggered 라 거기서의 평범한 ``await on_usage(...)`` 는 첫 Redis 왕복에서
    다시 잘린다. 웹서치를 켠 프로파일에서만 쓰이므로 꺼진 환경에서는 증상이 없지만, 켜는 순간
    같은 유실이 시작된다.
    """
    for name in ("_anthropic_stream", "_responses_stream"):
        blocks = _stitcher_finally_blocks(name)
        assert blocks, (
            f"{name} 에 finally 회계가 없다 — 구조가 바뀌었으면 이 트립와이어를 갱신할 것"
        )
        shielded = False
        for t in blocks:
            final = ast.Module(body=t.finalbody, type_ignores=[])
            if "bg_tasks.spawn_shielded" in _calls(final):
                shielded = True
            bare = [
                sub
                for sub in ast.walk(final)
                if isinstance(sub, ast.Await)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id == "on_usage"
            ]
            assert not bare, f"{name} 의 finally 가 on_usage 를 보호 없이 await 한다"
        assert shielded, f"{name} 의 finally 회계가 bg_tasks.spawn_shielded 를 거치지 않는다"


def test_web_search_has_no_weakly_referenced_tasks():
    src = _read("services/web_search_loop.py")
    assert "asyncio.create_task(" not in src, (
        "web_search_loop.py 에 asyncio.create_task 가 생겼다 — bg_tasks.spawn 을 쓸 것."
    )


def test_web_search_body_log_wrapper_detaches_on_cancel():
    """웹서치 본문 로그는 스티처 밖의 래퍼가 쏜다 — 취소에도 떼어 낸 태스크로 남겨야 한다.

    스티처의 usage 를 보호해도 ``CancelledError`` 는 래퍼까지 올라온다. 래퍼가 그 예외를 처리하지
    않으면 끊김이 회계 도중에 떨어진 요청의 본문 로그가 사라진다.
    """
    src = _read("services/body_log_records.py")
    assert re.search(r"except\s*\(\s*asyncio\.CancelledError\s*,\s*GeneratorExit\s*\)", src), (
        "wrap_stream_for_body_log 가 CancelledError 를 GeneratorExit 과 같이 처리하지 않는다"
    )
    assert "bg_tasks.spawn(" in src and "ensure_future(" not in src, (
        "본문 로그 발화가 bg_tasks 밖의 태스크로 떼어진다 — 셧다운 drain 대상에서 빠진다"
    )


# ── 3. routers/bedrock.py — /model/* 패스스루 ─────────────────────────────────


def test_bedrock_passthrough_finalize_is_shielded():
    """``/model/*/invoke-with-response-stream`` 패스스루.

    이 경로의 회계는 ``finally`` 안에서 await 된다. 취소 때문에 도는 ``finally`` 의 await 는
    그 자체가 다시 취소되므로, 보호 없이는 종결 청크에서 끊는 클라이언트가 차감만 남기고 원장을
    비운다.
    """
    src = _read("routers/bedrock.py")
    assert not re.search(r"await\s+cost_recorder\.finalize\(", src), (
        "bedrock.py 가 cost_recorder.finalize 를 보호 없이 await 한다 — "
        "bg_tasks.spawn_shielded 로 감쌀 것."
    )
    assert src.count("bg_tasks.spawn_shielded(") == 2, (
        "스트림·비스트림 두 경로가 모두 보호돼야 한다"
    )


# ── 4. main.py — 셧다운 순서 ──────────────────────────────────────────────────


def test_shutdown_drains_background_tasks_before_closing_their_sinks():
    """``bg_tasks.drain()`` 은 회계가 쓰는 **모든 싱크보다 먼저** 와야 한다.

    shield 는 *요청* 취소만 막는다. 프로세스 셧다운에는 drain 이 필요하고, 그 drain 이 싱크보다
    늦으면 같은 유실이 롤링 배포마다 일어난다:
      · body_logger.stop() — flusher 를 취소한 뒤 큐를 비운다. 그 뒤에 들어온 본문 로그는 죽은
        flusher 뒤에 쌓여 조용히 버려진다.
      · redis.aclose() / db_engine.dispose() — finalize 가 차감과 XADD 에 쓴다.
      · cost_stream_spool.drain() — 이 태스크들이 spool 한 레코드가 마지막 재발행을 받아야 한다.
    """
    # 주석이 아니라 실제 호출 위치를 AST 로 잡는다 — 설명 주석에 같은 마커 문자열이 있다.
    tree = ast.parse(_read("main.py"))

    def _call_line(obj: str, attr: str) -> int:
        # obj 까지 맞춘다 — 같은 메서드명이 여러 객체에 있다(httpx_client.aclose / redis.aclose).
        hits = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == obj
        ]
        assert hits, f"셧다운 호출을 찾을 수 없다: {obj}.{attr}"
        return min(hits)

    drain = _call_line("bg_tasks", "drain")
    for obj, attr in (
        ("body_logger", "stop"),
        ("cost_stream_spool", "drain"),
        ("redis", "aclose"),
        ("db_engine", "dispose"),
    ):
        assert drain < _call_line(obj, attr), (
            f"bg_tasks.drain() 이 {obj}.{attr}() 보다 뒤에 있다 — "
            "셧다운 중 회계가 닫힌 싱크에 쓴다."
        )


# ── 5. 단일 판정점 — 예약과 청구가 같은 함수를 쓴다 ────────────────────────────


def test_cost_estimator_reuses_the_billing_tier_decision():
    """pre-reserve 추정기가 272K 판정을 **재구현하지 않는다**.

    각자 비교하면 한쪽만 고쳐졌을 때 "예약은 short, 청구는 long" 으로 갈라지고, 그 갈라짐은 로그에
    남지 않는다(settle 이 사후에 숫자를 맞춰 원장은 정상으로 보인다).
    """
    src = _read("services/rate_limit_enforcement.py")
    assert "resolve_tier_for_prompt_total" in src, (
        "추정기가 cost_recorder 의 티어 판정을 쓰지 않는다 — 임계값 비교가 두 곳으로 갈라졌다."
    )
    assert not re.search(r"long_context_threshold_tokens\s*(?:is|==|>|<)", src), (
        "추정기가 임계값을 직접 비교한다 — 판정은 "
        "cost_recorder.resolve_tier_for_prompt_total 한 곳."
    )


def test_billing_path_delegates_to_the_shared_threshold_comparison():
    """반대 방향도 고정한다 — ``resolve_context_tier`` 가 자기 비교를 들고 있으면 안 된다."""
    fns = _functions(_read("services/cost_recorder.py"))
    tier = fns.get("resolve_context_tier", [])
    assert len(tier) == 1, "resolve_context_tier 가 하나여야 한다"
    assert "resolve_tier_for_prompt_total" in _calls(tier[0]), (
        "resolve_context_tier 가 공용 비교 함수에 위임하지 않는다 — 추정기와 갈라질 수 있다."
    )
