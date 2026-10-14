# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`POST /admin/chat/sql/execute` 는 Lambda 크래시를 **절대 성공으로 돌려주지 않는다**.

고정하는 결함 — 크래시가 "0행" 으로 보였다:
라우트는 `json.loads(resp["Payload"].read())` 만 하고 `resp["FunctionError"]` 를 보지 않았다.
핸들러가 죽거나 타임아웃하면 Lambda 는 **호출 성공**으로 응답하면서 payload 에
`{"errorMessage","errorType","stackTrace"}` 를 담는다. 그 dict 에는 `ok` 가 없으므로 admin-ui 의
유일한 에러 게이트(`result.ok === false`)가 통과하고, SQL 에디터는 성공 분기를 그린다: 행 없음 ·
행 수 없음 · 에러 문구 없음 = "0행". 분석가는 "이 데이터는 존재하지 않는다" 로 읽는다.

502 로 안 잡히는 이유가 핵심이다: Lambda 의 wall timeout 은 boto3 기본 read_timeout(60s)보다
짧다. 타임아웃은 ReadTimeoutError 가 아니라 **정상 응답 + FunctionError** 로 돌아온다.

정상 반환은 손대지 않는다: Lambda 가 스스로 판단한 `ok:false`(화이트리스트 위반/DML 등)는
에디터 UX 의 핵심이므로 200 + 그대로다.
"""
from __future__ import annotations

import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.routers.chat_agent import SqlExecuteRequest, execute_sql, normalize_lambda_result

SESSION_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _lambda_response(payload: dict | bytes, *, function_error: str | None = None) -> dict:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    resp: dict = {"Payload": io.BytesIO(raw)}
    if function_error:
        resp["FunctionError"] = function_error
    return resp


class TestNormalizeLambdaResult:
    def test_unhandled_timeout_becomes_ok_false_with_the_message(self):
        resp = _lambda_response(
            {
                "errorMessage": "Task timed out after 30.00 seconds",
                "errorType": "Sandbox.Timedout",
                "stackTrace": [],
            },
            function_error="Unhandled",
        )
        out = normalize_lambda_result(resp)
        assert out["ok"] is False
        assert "Task timed out" in out["error"]
        # 원본 크래시 봉투가 그대로 새어나가면 클라이언트가 성공으로 읽는다.
        assert "errorMessage" not in out
        assert out.get("hint")

    def test_oom_exit_error_still_yields_a_message(self):
        resp = _lambda_response(
            {"errorMessage": "Runtime exited with error: signal: killed", "errorType": "Runtime.ExitError"},
            function_error="Unhandled",
        )
        out = normalize_lambda_result(resp)
        assert out["ok"] is False
        assert "Runtime exited" in out["error"]

    def test_function_error_with_unparseable_payload_still_fails_loudly(self):
        out = normalize_lambda_result(_lambda_response(b"<not json>", function_error="Unhandled"))
        assert out["ok"] is False
        assert "Unhandled" in out["error"]

    def test_non_dict_payload_without_function_error_is_not_a_success(self):
        """계약 위반(리스트/문자열 반환)을 성공으로 그리는 것보다 시끄럽게."""
        out = normalize_lambda_result(_lambda_response(b"[1, 2, 3]"))
        assert out["ok"] is False

    def test_handled_validation_failure_passes_through_untouched(self):
        body = {"ok": False, "error": "table not in whitelist", "hint": "use usage.usage_logs"}
        assert normalize_lambda_result(_lambda_response(body)) == body

    def test_successful_result_passes_through_untouched(self):
        body = {"ok": True, "rows": [{"n": 1}], "row_count": 1}
        assert normalize_lambda_result(_lambda_response(body)) == body

    def test_genuine_zero_row_success_is_still_a_success(self):
        """진짜 0행과 크래시를 갈라야 한다 — 예전 코드가 붕괴시킨 바로 그 구분이다."""
        body = {"ok": True, "rows": [], "row_count": 0}
        assert normalize_lambda_result(_lambda_response(body)) == body


class TestRouteReturnsNormalizedEnvelope:
    """라우트 전체 경로 — boto3 를 가짜로 바꿔 crash payload 를 흘려 넣는다."""

    @staticmethod
    def _session() -> AsyncMock:
        row = MagicMock()
        row.user_id = USER_ID
        result = MagicMock()
        result.first = MagicMock(return_value=row)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        return session

    @staticmethod
    def _admin(user_id: uuid.UUID = USER_ID) -> CurrentUser:
        return CurrentUser(user_id=user_id, email="admin@example.com", role=UserRole.ADMIN, team_id=None)

    @pytest.fixture
    def fake_boto3(self, monkeypatch):
        import boto3

        holder: dict = {}

        def _client(*_args, **_kwargs):
            client = MagicMock()
            client.invoke = MagicMock(side_effect=lambda **_kw: holder["response"])
            return client

        monkeypatch.setattr(boto3, "client", _client)
        return holder

    async def test_lambda_crash_is_returned_as_ok_false(self, fake_boto3):
        fake_boto3["response"] = _lambda_response(
            {"errorMessage": "Task timed out after 30.00 seconds", "errorType": "Sandbox.Timedout"},
            function_error="Unhandled",
        )
        body = await execute_sql(
            SqlExecuteRequest(sql="SELECT 1", session_id=SESSION_ID),
            admin=self._admin(),
            session=self._session(),
        )
        assert body["ok"] is False
        assert "Task timed out" in body["error"]

    async def test_successful_query_is_returned_verbatim(self, fake_boto3):
        fake_boto3["response"] = _lambda_response({"ok": True, "rows": [{"c": 3}], "row_count": 1})
        body = await execute_sql(
            SqlExecuteRequest(sql="SELECT 1", session_id=SESSION_ID),
            admin=self._admin(),
            session=self._session(),
        )
        assert body == {"ok": True, "rows": [{"c": 3}], "row_count": 1}

    async def test_session_ownership_is_still_enforced(self, fake_boto3):
        """정규화를 추가하면서 소유 검증을 건드리지 않았다."""
        from fastapi import HTTPException

        fake_boto3["response"] = _lambda_response({"ok": True, "rows": []})
        with pytest.raises(HTTPException) as exc:
            await execute_sql(
                SqlExecuteRequest(sql="SELECT 1", session_id=SESSION_ID),
                admin=self._admin(uuid.UUID("33333333-3333-4333-8333-333333333333")),
                session=self._session(),
            )
        assert exc.value.status_code == 403
