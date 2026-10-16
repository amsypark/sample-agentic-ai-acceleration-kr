# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CostStreamEntry 스키마 — gateway-proxy `schemas/cost_stream.py` 의 거울본.

gateway가 XADD 하는 payload를 worker가 JSON으로 역직렬화 → Pydantic 검증 →
DB writer 로 넘기는 흐름. 양쪽 파일을 동기화 상태로 유지 (지금은 수동, 향후
단일 소스로 통합 가능).
"""
from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

# usage_logs.aws_account_id 는 VARCHAR(12) 다(0045). 형식이 어긋난 값 하나가 INSERT 를 실패시키면
# 그 배치 전체가 기록되지 않으므로(XACK 안 됨 → 재시도 반복) 여기서 걸러 None 으로 둔다.
_ACCOUNT_ID_RE = re.compile(r"[0-9]{12}")


class CostStreamEntry(BaseModel):
    request_id: str
    user_id: str
    team_id: str
    dept_id: str
    model_alias: str
    provider: str

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Reasoning/thinking tokens — visibility submetric (already inside output_tokens).
    # NOT a billing input; persisted to usage_logs.reasoning_tokens for analytics/UI.
    reasoning_tokens: int = 0
    # Server-side web search calls for this request — attribution metric, NOT billing.
    # Persisted to usage_logs.web_search_count for per-client AgentCore search analytics.
    web_search_count: int = 0
    cost_usd: Decimal

    latency_ms: int
    # TTFT(time to first token) in ms. 스트리밍 첫 콘텐츠 델타까지의 시간.
    # 비스트리밍/미검출은 latency_ms와 동일 값. 구버전(schema_version 1) 엔트리는 부재 → None.
    ttft_ms: int | None = None
    is_streaming: bool = False
    estimated_usage: bool = False
    downgraded_from: str | None = None
    availability_fallback_from: str | None = None

    requested_at: str  # ISO format
    completed_at: str
    period: str  # YYYY-MM
    date: str  # YYYY-MM-DD

    threshold_triggered: int | None = None
    threshold_policy: str | None = None

    sso_subject: str | None = None  # OIDC sub or stable user identifier
    bedrock_request_id: str | None = None
    # Identification tag "claude-code" | "cowork" | "codex" | "other". Was previously
    # dropped here (Pydantic ignored the extra field) -> NULL client logs; now carried
    # through so per-client analytics/dashboards see all three apps.
    client: str | None = None
    # 청구 티어 감사 (마이그레이션 0039) — 게이트웨이가 채운 값을 그대로 usage_logs 에 쓴다.
    context_tier: str | None = None
    # 이 요청을 실제로 서빙한 AWS 계정 12자리 (마이그레이션 0045). None = 게이트웨이 자신의
    # 계정(또는 0045 이전 gateway 의 엔트리). 기본 None 이라 필드 없는 엔트리도 파싱된다.
    aws_account_id: str | None = None

    @field_validator("aws_account_id", mode="before")
    @classmethod
    def _drop_malformed_account_id(cls, v):
        # 감사용 부가 정보다 — 과금 행을 잃느니 계정만 비운다.
        if isinstance(v, str) and _ACCOUNT_ID_RE.fullmatch(v):
            return v
        return None

    schema_version: int = Field(default=2)
