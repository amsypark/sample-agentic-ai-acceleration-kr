# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


# ── Requests ──


class RateLimitSetRequest(BaseModel):
    rpm: int | None = Field(None, ge=1, description="Requests per minute")
    tpm: int | None = Field(None, ge=1, description="Tokens per minute")
    cpm: Decimal | None = Field(None, ge=0, description="Cost per minute (USD), USER/TEAM scope")
    cph: Decimal | None = Field(None, ge=0, description="Cost per hour (USD), USER/TEAM scope")


# ── Responses ──


class RateLimitDeleteResponse(BaseModel):
    """USER 오버라이드 제거 결과. ``deactivated`` = 비활성화된 활성 행 수(0 = 원래 없었음, 멱등)."""

    scope: str
    scope_id: str
    deactivated: int


class RateLimitResponse(BaseModel):
    scope: str
    scope_id: str | None = None
    model_alias: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    cpm_limit_usd: Decimal | None = None
    cph_limit_usd: Decimal | None = None
    is_active: bool


# ── Rate Limit Tree ──


class RateLimitConfigItem(BaseModel):
    """트리 노드의 실효 한도.

    ⚠️ ``cpm``/``cph`` 는 ``Decimal`` 이라 **JSON 문자열**로 나간다(NUMERIC(10,4) → ``"0.5000"``).
    같은 객체의 ``rpm``/``tpm`` 은 JSON 숫자다. admin-ui ``types/entities.ts`` 의 RateLimitConfig
    (``cpm: string | null``)가 이 비대칭을 반영하고 소비 지점에서 숫자로 바꾼다. 돈 값의 정밀도를
    잃지 않으려고 서버를 ``float`` 으로 바꾸지 않는다 — 바꾸면 저쪽 타입이 조용히 거짓이 된다.
    ``scope`` 는 이 값의 **출처** 스코프다 — 노드 scope 와 다르면 상속된 값이다.
    """

    target_id: str
    scope: str
    rpm: int | None = None
    tpm: int | None = None
    cpm: Decimal | None = None
    cph: Decimal | None = None


class RateLimitTreeNode(BaseModel):
    id: str
    label: str
    scope: str  # USER | TEAM | GLOBAL
    is_active: bool = True
    config: RateLimitConfigItem | None = None
    children: list["RateLimitTreeNode"] = []
    inherited_from: str | None = None


RateLimitTreeNode.model_rebuild()
