// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';
import { resolveMonth } from '@/lib/utils/period';

/**
 * `/admin/analytics` 질의 파라미터 빌더 — **단일 출처**.
 *
 * analytics 페이지의 여러 서버 컴포넌트(ROIMetricsCards/CostTrendChart/BreakdownChart)
 * 와 퀵챗 컨텍스트 등록(ContextSection)이 모두 같은 엔드포인트를 같은 파라미터로
 * 호출한다. Next.js **요청 메모이제이션**은 동일 렌더 내 동일 URL+옵션 fetch 를
 * 1회로 dedup 하므로, 모든 호출처가 이 함수를 거쳐 **바이트 동일 요청**을 만들어야
 * 중복 네트워크 호출이 발생하지 않는다(파라미터가 갈리면 dedup 이 조용히 깨짐).
 */
export function buildAnalyticsQuery(
  filter: AnalyticsFilterForm,
  latestMonth?: string,
): Record<string, string | number | undefined> {
  const query: Record<string, string | number | undefined> = {
    period: resolveMonth(filter, latestMonth),
    group_by: filter.group_by,
    scope: filter.scope ?? 'all',
  };

  // `직접 입력` 날짜 범위. 예전에는 선택기가 URL 에만 날짜를 쓰고 이 빌더는 월로 접어
  // (period=start_date.slice(0,7)) 보냈고, 서버는 선언조차 없는 파라미터를 조용히 버렸다 →
  // 5일 범위를 고른 운영자가 **한 달치** 비용을 5일치로 읽었다. 이제 서버가 받는다
  // (admin-api routers/analytics.py start_date/end_date).
  //
  // ⚠️ 둘 다 있을 때만 보낸다. 한쪽만 보내면 서버가 400 으로 거절하므로, 날짜를 하나만 고른
  //    중간 상태에서 화면이 에러로 깨지는 대신 그 달 집계를 계속 보여준다(기간 라벨이
  //    `2026-09-01 ~ ?` 로 미완성임을 표시한다 — app/analytics/page.tsx).
  //    period 는 그대로 함께 보낸다 — 서버 필수 파라미터이고 캐시 키·응답 라벨에 쓰인다.
  if (filter.period === 'custom' && filter.start_date && filter.end_date) {
    query.start_date = filter.start_date;
    query.end_date = filter.end_date;
  }

  return query;
}
