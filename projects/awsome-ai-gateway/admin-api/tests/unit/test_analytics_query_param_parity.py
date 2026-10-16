# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`/admin/analytics/export` 의 쿼리 파라미터는 `/admin/analytics` 의 상위집합이어야 한다.

이것이 이 결함군의 **드리프트 감지기**다. FastAPI 는 선언되지 않은 쿼리 파라미터를
422 없이 버리므로, 화면 라우트가 필터를 하나 더 갖게 되는 순간 export 는 그 필터를
조용히 무시하고 다른 데이터셋을 파일로 내려준다(scope 와 날짜 범위가 정확히 그렇게 빠져
있었다). 타입 검사도, 런타임 에러도 이걸 잡지 못한다 — 그래서 테스트로 못박는다.

파라미터 목록은 FastAPI 자신의 의존성 분석(`get_dependant`)으로 읽는다. 시그니처의 기본값
타입으로 추측하면 `Annotated[..., Query()] = None` 형태(기본값이 진짜 None)를 놓친다.
"""
from __future__ import annotations

from fastapi.dependencies.utils import get_dependant

from app.routers.analytics import export_analytics, get_analytics
from app.schemas.analytics import AnalyticsQueryParams, ExportParams


def _query_params(fn) -> set[str]:
    return {p.name for p in get_dependant(path="/", call=fn).query_params}


def test_helper_sees_the_annotated_range_params():
    """대조군 — 헬퍼가 Annotated 로 선언된 범위 파라미터를 실제로 읽는가(아니면 아래가 공허)."""
    assert {"start_date", "end_date"} <= _query_params(get_analytics)


def test_export_accepts_every_filter_the_screen_route_accepts():
    screen = _query_params(get_analytics)
    export = _query_params(export_analytics)

    assert screen, "화면 라우트의 쿼리 파라미터를 못 읽었다 — 헬퍼가 깨졌다"
    missing = screen - export
    assert not missing, f"export 라우트에 없는 화면 필터: {sorted(missing)} (조용히 드롭된다)"
    # export 고유 파라미터는 format 하나뿐이어야 한다(반대 방향 드리프트 감지).
    assert export - screen == {"format"}


def test_declared_param_schemas_mirror_the_routes():
    """schemas 의 쿼리 파라미터 모델도 같이 늙지 않게 한다(문서/OpenAPI 용)."""
    assert set(AnalyticsQueryParams.model_fields) == _query_params(get_analytics)
    assert set(ExportParams.model_fields) == _query_params(export_analytics)
    assert set(AnalyticsQueryParams.model_fields) <= set(ExportParams.model_fields)
