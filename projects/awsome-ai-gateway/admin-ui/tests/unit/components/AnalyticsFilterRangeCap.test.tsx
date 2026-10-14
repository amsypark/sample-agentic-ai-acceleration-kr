// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// AnalyticsFilter — `직접 입력` 범위가 서버 상한(93일)을 넘으면 선택기 옆에 이유를 적는다.
// 서버는 400 으로 거절하고 카드/차트는 "불러오지 못했습니다" 만 보이므로, 이 안내가 없으면
// 운영자는 왜 비었는지 알 수 없다.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { AnalyticsFilter } from '@/components/analytics/AnalyticsFilter';
import type { AnalyticsFilterForm } from '@/types/api';
import type { PeriodType } from '@/types/enums';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/analytics',
}));

function renderFilter(start: string | null, end: string | null) {
  const value: AnalyticsFilterForm = {
    period: 'custom' as PeriodType,
    group_by: 'model',
    start_date: start,
    end_date: end,
    scope: null,
  };
  return render(
    <NextIntlClientProvider locale="ko" messages={{}} onError={() => {}}>
      <AnalyticsFilter defaultValue={value} periods={[]} currentMonth="2026-09" />
    </NextIntlClientProvider>,
  );
}

describe('AnalyticsFilter range cap', () => {
  it('warns when the custom range is longer than the server accepts', () => {
    renderFilter('2026-09-01', '2026-12-03');
    expect(screen.getByRole('alert').textContent).toContain('최대 93일');
    expect(screen.getByRole('alert').textContent).toContain('94일');
  });

  it('stays quiet at the cap and for a half-filled range (control)', () => {
    const { unmount } = renderFilter('2026-09-01', '2026-12-02');
    expect(screen.queryByRole('alert')).toBeNull();
    unmount();
    renderFilter('2026-09-01', null);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
