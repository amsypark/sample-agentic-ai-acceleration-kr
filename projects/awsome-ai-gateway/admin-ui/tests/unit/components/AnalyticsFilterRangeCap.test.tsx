// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// AnalyticsFilter — `직접 입력` 범위가 서버 상한(93일)을 넘으면 선택기 옆에 이유를 적는다.
// 서버는 400 으로 거절하고 카드/차트는 "불러오지 못했습니다" 만 보이므로, 이 안내가 없으면
// 운영자는 왜 비었는지 알 수 없다.
//
// ⚠️ 안내 문구는 메시지 카탈로그(messages/*.json)에서 온다 — 처음엔 한국어로 하드코딩되어 en 로케일에서도
//    한국어로 나왔다. 그래서 여기서는 빈 messages 가 아니라 **실제 카탈로그**를 넣고 두 로케일을 다 본다.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { AnalyticsFilter } from '@/components/analytics/AnalyticsFilter';
import type { AnalyticsFilterForm } from '@/types/api';
import type { PeriodType } from '@/types/enums';
import ko from '../../../messages/ko.json';
import en from '../../../messages/en.json';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/analytics',
}));

// 카탈로그에는 배열 값(chat.suggestions)이 있어 JSON 타입이 AbstractIntlMessages 와 맞지 않는다 — 앱은
// getMessages() 로 같은 파일을 넘긴다.
const CATALOGS = { ko, en } as unknown as Record<'ko' | 'en', AbstractIntlMessages>;

function renderFilter(start: string | null, end: string | null, locale: 'ko' | 'en' = 'ko') {
  const value: AnalyticsFilterForm = {
    period: 'custom' as PeriodType,
    group_by: 'model',
    start_date: start,
    end_date: end,
    scope: null,
  };
  return render(
    <NextIntlClientProvider locale={locale} messages={CATALOGS[locale]} onError={() => {}}>
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

  it('says it in the active locale — English, not hard-coded Korean', () => {
    renderFilter('2026-09-01', '2026-12-03', 'en');
    const text = screen.getByRole('alert').textContent ?? '';
    expect(text).toContain('at most 93 days');
    expect(text).toContain('selected: 94 days');
    expect(text).not.toMatch(/[\uAC00-\uD7A3]/);
  });

  it('both catalogs carry the message with the same placeholders', () => {
    for (const catalog of [ko, en]) {
      const message = (catalog.analytics as Record<string, unknown>).rangeTooLong;
      expect(typeof message).toBe('string');
      expect(message).toContain('{max}');
      expect(message).toContain('{days}');
    }
  });

  it('stays quiet at the cap and for a half-filled range (control)', () => {
    const { unmount } = renderFilter('2026-09-01', '2026-12-02');
    expect(screen.queryByRole('alert')).toBeNull();
    unmount();
    renderFilter('2026-09-01', null);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
