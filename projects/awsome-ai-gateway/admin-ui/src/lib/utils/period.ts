// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';

/**
 * 현재 **KST** 연/월/일. 실행 환경 TZ 와 무관하다.
 *
 * ⚠️ 백엔드의 모든 집계는 KST 버킷(§59 cost_period_filter / kst_month_expr)인데,
 *    예전 헬퍼들은 `new Date().getMonth()` 같은 **로컬 시간**을 썼다. 서버 컴포넌트에서는
 *    그 로컬이 pod 의 UTC 라서 매일 00:00~09:00 KST 구간에 하루 전 날짜/달을 답했다
 *    (일평균 분모 off-by-one, 매월 1일 09시까지는 월말 예상이 통째로 사라짐).
 *    클라이언트에서는 열람자의 TZ 라 같은 화면이 사람마다 달랐다. Intl 로 뽑으면
 *    서버/브라우저 양쪽에서 동일하다.
 */
export function kstNowParts(): { y: number; m: number; d: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const get = (type: string) => Number(parts.find((p) => p.type === type)!.value);
  return { y: get('year'), m: get('month'), d: get('day') };
}

/** 현재 달력월 'YYYY-MM' (KST 기준 — 백엔드 집계 기준과 일치). */
export function currentCalendarMonth(): string {
  const { y, m } = kstNowParts();
  return `${y}-${String(m).padStart(2, '0')}`;
}

/** N개월 전 달 'YYYY-MM' (0=이번 달, 1=지난 달). KST 기준. */
export function monthsAgo(n: number): string {
  const { y, m } = kstNowParts();
  // UTC 산술 — 로컬 TZ 가 개입하지 않는 순수 달력 계산.
  const d = new Date(Date.UTC(y, m - 1 - n, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

/** 문자열이 'YYYY-MM' 월 형식인지. */
export function isMonth(value: string | null | undefined): value is string {
  return !!value && /^\d{4}-\d{2}$/.test(value);
}

/** 'YYYY-MM' → 'YYYY년 M월'. 순수 문자열 split (new Date 금지 — TZ drift). */
export function toKoreanMonthLabel(p: string): string {
  const [y, m] = p.split('-');
  return `${y}년 ${Number(m)}월`;
}

/**
 * 'YYYY-MM' → 콘솔 로케일의 월 이름(ko '2026년 8월', en 'August 2026').
 * 그 달 1일의 UTC 자정으로 만들고 UTC 로 포맷한다 — 로컬 시간대로 포맷하면 음수 오프셋에서 전월이 된다.
 */
export function formatMonthLabel(p: string, locale: string): string {
  const [y, m] = p.split('-').map(Number);
  return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', timeZone: 'UTC' }).format(
    new Date(Date.UTC(y, m - 1, 1)),
  );
}

/**
 * 절대 월을 상대 레이블로. 이번 달/지난 달이면 그 레이블, 아니면 'YYYY년 M월'.
 * 시간이 흘러도(7월·8월…) 안정적인 표기.
 */
export function relativeMonthLabel(month: string): string {
  if (month === currentCalendarMonth()) return '이번 달';
  if (month === monthsAgo(1)) return '지난 달';
  return toKoreanMonthLabel(month);
}

/**
 * analytics 필터를 백엔드가 기대하는 단일 월(YYYY-MM)로 환산.
 *
 * 백엔드 /admin/analytics 는 to_char(requested_at,'YYYY-MM')==period 로
 * 단일 월만 필터한다. 필터의 7d/30d/90d 상대 기간은 월 단위로 환산되며,
 * custom 이 아닐 때 "현재 달력월" 대신 latestWithData(데이터 있는 최근 월)를
 * 기본으로 써서 월이 바뀌어도 빈 화면을 피한다.
 *
 * @param filter         analytics 필터 폼
 * @param latestWithData 데이터가 있는 가장 최근 월. 미지정 시 현재 달력월.
 */
export function resolveMonth(filter: AnalyticsFilterForm, latestWithData?: string): string {
  // 명시적 YYYY-MM 월이 흘러들어오면 존중(월 선택기 경로).
  if (isMonth(filter.period as unknown as string)) {
    return filter.period as unknown as string;
  }
  if (filter.period === 'custom' && filter.start_date) {
    return filter.start_date.slice(0, 7);
  }
  return latestWithData ?? currentCalendarMonth();
}
