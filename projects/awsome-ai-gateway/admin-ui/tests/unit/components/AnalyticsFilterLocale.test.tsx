// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// AnalyticsFilter 의 월 선택기는 콘솔 로케일을 따른다 — 버튼(이번 달 / 지난 달 / 직접 입력), 드롭다운의 안내
// 문구와 aria-label, 월 이름까지. 예전엔 전부 한국어로 하드코딩되어(월 이름은 '2026년 8월' 고정) 영어 콘솔에서도
// 한국어로 나왔다. 실제 메시지 카탈로그(messages/*.json)로 두 로케일을 다 본다.

import { render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

// 카탈로그에는 배열 값(chat.suggestions)이 있어 JSON 타입이 AbstractIntlMessages 와 맞지 않는다.
const CATALOGS = { ko, en } as unknown as Record<'ko' | 'en', AbstractIntlMessages>;
const HANGUL = /[가-힣]/;
const KEYS = ['thisMonth', 'lastMonth', 'monthPicker', 'monthPickerPlaceholder', 'customRange'] as const;

function renderFilter(locale: 'ko' | 'en') {
  const value: AnalyticsFilterForm = {
    period: '2026-10' as PeriodType,
    group_by: 'model',
    start_date: null,
    end_date: null,
    scope: null,
  };
  return render(
    <NextIntlClientProvider locale={locale} messages={CATALOGS[locale]} onError={() => {}}>
      <AnalyticsFilter
        defaultValue={value}
        periods={['2026-10', '2026-09', '2026-08', '2026-07']}
        currentMonth="2026-10"
      />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-15T03:00:00Z')); // KST 2026-10-15 — 이번 달 10월, 지난 달 9월
});

afterEach(() => {
  vi.useRealTimers();
});

describe('AnalyticsFilter month picker locale', () => {
  it('English console — buttons, picker and month names are English', () => {
    const { container } = renderFilter('en');
    expect(screen.getByRole('button', { name: 'This month' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Last month' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Custom range' })).toBeInTheDocument();
    const picker = screen.getByRole('combobox', { name: 'Select a month' });
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Select period',
      'August 2026',
      'July 2026',
    ]);
    // 보이는 글자와 aria-label 어디에도 한국어가 남지 않는다
    expect(container.textContent).not.toMatch(HANGUL);
    for (const el of container.querySelectorAll('[aria-label]')) {
      expect(el.getAttribute('aria-label')).not.toMatch(HANGUL);
    }
  });

  it('Korean console keeps the existing wording (control)', () => {
    renderFilter('ko');
    expect(screen.getByRole('button', { name: '이번 달' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '지난 달' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '직접 입력' })).toBeInTheDocument();
    const picker = screen.getByRole('combobox', { name: '기간 선택 (월)' });
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual([
      '기간 선택',
      '2026년 8월',
      '2026년 7월',
    ]);
  });

  it('both catalogs carry every month-picker key', () => {
    for (const catalog of [ko, en]) {
      const analytics = catalog.analytics as Record<string, unknown>;
      for (const key of KEYS) expect(typeof analytics[key], key).toBe('string');
    }
  });
});
