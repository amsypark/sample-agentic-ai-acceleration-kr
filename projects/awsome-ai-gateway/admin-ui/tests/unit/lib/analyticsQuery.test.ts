// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// buildAnalyticsQuery — /admin/analytics 질의의 **단일 출처**.
//
// 결함: `직접 입력`(시작일/종료일) 선택기는 날짜를 URL 에 넣었지만 이 빌더는 그것을 월로
// 접어(period=start_date.slice(0,7)) 날짜를 아예 보내지 않았다. 서버도 그 파라미터를
// 선언하지 않아 조용히 버렸으므로, 5일 범위를 고른 운영자가 **한 달치** 비용을 5일치로
// 읽었다(카드 설명은 "기간 내 …", 선택기는 고른 날짜를 그대로 표시).

import { describe, it, expect } from 'vitest';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';
import type { AnalyticsFilterForm } from '@/types/api';
import type { PeriodType } from '@/types/enums';

function filter(overrides: Partial<AnalyticsFilterForm> = {}): AnalyticsFilterForm {
  return {
    period: '2026-09' as PeriodType,
    group_by: 'model',
    start_date: null,
    end_date: null,
    scope: null,
    ...overrides,
  };
}

describe('buildAnalyticsQuery', () => {
  it('forwards a custom date range to the API', () => {
    const q = buildAnalyticsQuery(
      filter({ period: 'custom' as PeriodType, start_date: '2026-09-01', end_date: '2026-09-05' }),
      '2026-09',
    );

    expect(q.start_date).toBe('2026-09-01');
    expect(q.end_date).toBe('2026-09-05');
    // period 는 계속 보낸다 — 서버 필수 파라미터(캐시 키/응답 라벨).
    expect(q.period).toBe('2026-09');
    expect(q.group_by).toBe('model');
    expect(q.scope).toBe('all');
  });

  it('omits the range for a month selection (control)', () => {
    const q = buildAnalyticsQuery(filter({ period: '2026-08' as PeriodType }), '2026-09');
    expect(q.period).toBe('2026-08');
    expect(q.start_date).toBeUndefined();
    expect(q.end_date).toBeUndefined();
  });

  it('ignores stray dates when not in custom mode', () => {
    // 월 선택기로 돌아오면 URL 에 이전 날짜가 남아 있을 수 있다 — 월 질의가 범위로 바뀌면 안 된다.
    const q = buildAnalyticsQuery(
      filter({ period: '2026-08' as PeriodType, start_date: '2026-09-01', end_date: '2026-09-05' }),
      '2026-09',
    );
    expect(q.start_date).toBeUndefined();
    expect(q.period).toBe('2026-08');
  });

  it('omits a half-filled range (server rejects one-sided ranges with 400)', () => {
    const q = buildAnalyticsQuery(
      filter({ period: 'custom' as PeriodType, start_date: '2026-09-01', end_date: null }),
      '2026-09',
    );
    expect(q.start_date).toBeUndefined();
    expect(q.end_date).toBeUndefined();
    // 범위를 못 보내는 동안은 그 달 집계를 계속 보여준다.
    expect(q.period).toBe('2026-09');
  });

  it('keeps an explicit scope', () => {
    const q = buildAnalyticsQuery(filter({ scope: 'team:abc' }), '2026-09');
    expect(q.scope).toBe('team:abc');
  });

  it('derives the period month from start_date in custom mode', () => {
    const q = buildAnalyticsQuery(
      filter({ period: 'custom' as PeriodType, start_date: '2026-07-20', end_date: '2026-07-25' }),
      '2026-09',
    );
    expect(q.period).toBe('2026-07');
  });
});
