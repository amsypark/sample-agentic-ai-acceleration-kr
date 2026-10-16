// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// ExportButton — 내보내기 질의는 **화면 질의와 같아야** 한다.
//
// 결함: 이 컴포넌트가 파라미터를 손으로 조립해서 화면 빌더(buildAnalyticsQuery)와 갈렸다.
// 날짜를 하나만 고른 중간 상태에서는 start_date 하나만 보내 서버가 400 으로 거절했고(화면은
// 그 달 집계를 보여주는데 내보내기만 실패), 서버는 scope/start_date/end_date 를 선언조차 하지
// 않아 범위·팀 범위 화면에서 월 전체·전사 CSV 가 내려갔다. 여기서는 "두 질의가 같다" 를
// 못박아 빌더가 다시 갈라지는 것을 기계적으로 금지한다.

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { AnalyticsFilterForm } from '@/types/api';
import type { PeriodType } from '@/types/enums';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

import { ExportButton } from '@/components/analytics/ExportButton';

const CUSTOM_FILTER: AnalyticsFilterForm = {
  period: 'custom' as PeriodType,
  group_by: 'team',
  start_date: '2026-09-01',
  end_date: '2026-09-05',
  scope: 'team:11111111-1111-4111-8111-111111111111',
};

function mockFetch(headers: Record<string, string> = {}) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    blob: async () => new Blob(['period,team,cost_usd\n']),
    headers: { get: (k: string) => headers[k.toLowerCase()] ?? null },
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => {
  // jsdom 에는 없다 — blob 다운로드 경로가 여기서 죽지 않게 스텁.
  URL.createObjectURL = vi.fn(() => 'blob:mock');
  URL.revokeObjectURL = vi.fn();
  // jsdom 은 <a> 클릭을 네비게이션으로 보고 "Not implemented" 를 던진다 — 클릭 자체는 검증 대상이 아니다.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function clickExport(format: 'CSV' | 'JSON') {
  await userEvent.click(screen.getByRole('button', { name: /export/ }));
  await userEvent.click(screen.getByRole('menuitem', { name: format }));
}

function requestedParams(fetchMock: ReturnType<typeof vi.fn>): URLSearchParams {
  return new URL(fetchMock.mock.calls[0][0] as string, 'http://localhost').searchParams;
}

describe('ExportButton', () => {
  it('requests exactly the screen query plus format', async () => {
    const fetchMock = mockFetch();
    render(<ExportButton filter={CUSTOM_FILTER} latestMonth="2026-09" />);

    await clickExport('CSV');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const expected = new URLSearchParams(
      Object.entries(buildAnalyticsQuery(CUSTOM_FILTER, '2026-09')).map(([k, v]) => [k, String(v)]),
    );
    expected.set('format', 'csv');
    const got = requestedParams(fetchMock);
    // 두 질의가 같은 키·같은 값이어야 한다(순서 무관).
    expect(Object.fromEntries(got)).toEqual(Object.fromEntries(expected));
    // 회귀 고정: 이 세 파라미터가 빠지면 화면과 다른 데이터셋이 내려간다.
    expect(got.get('scope')).toBe(CUSTOM_FILTER.scope);
    expect(got.get('start_date')).toBe('2026-09-01');
    expect(got.get('end_date')).toBe('2026-09-05');
    expect(got.get('group_by')).toBe('team');
  });

  it('never sends a one-sided range (the server would reject it with 400)', async () => {
    const fetchMock = mockFetch();
    render(
      <ExportButton filter={{ ...CUSTOM_FILTER, end_date: null }} latestMonth="2026-09" />,
    );

    await clickExport('CSV');

    const got = requestedParams(fetchMock);
    expect(got.has('start_date')).toBe(false);
    expect(got.has('end_date')).toBe(false);
    expect(got.get('period')).toBe('2026-09');
  });

  it('always sends a scope even when the screen has none (defaults to all)', async () => {
    const fetchMock = mockFetch();
    render(
      <ExportButton
        filter={{ period: '2026-08' as PeriodType, group_by: 'model', scope: null }}
        latestMonth="2026-09"
      />,
    );

    await clickExport('JSON');

    const got = requestedParams(fetchMock);
    expect(got.get('scope')).toBe('all');
    expect(got.get('period')).toBe('2026-08');
    expect(got.get('format')).toBe('json');
    expect(got.has('start_date')).toBe(false);
  });

  it('names the file from the server Content-Disposition', async () => {
    mockFetch({ 'content-disposition': 'attachment; filename="analytics_2026-09-01_2026-09-05.csv"' });
    const anchors: HTMLAnchorElement[] = [];
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      const el = realCreate(tag);
      if (tag === 'a') anchors.push(el as HTMLAnchorElement);
      return el;
    }) as typeof document.createElement);

    render(<ExportButton filter={CUSTOM_FILTER} latestMonth="2026-09" />);
    await clickExport('CSV');

    const download = anchors.find((a) => a.href.startsWith('blob:'));
    expect(download?.download).toBe('analytics_2026-09-01_2026-09-05.csv');
  });

  it('falls back to a generic name when the server sends no filename (control)', async () => {
    mockFetch();
    const anchors: HTMLAnchorElement[] = [];
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      const el = realCreate(tag);
      if (tag === 'a') anchors.push(el as HTMLAnchorElement);
      return el;
    }) as typeof document.createElement);

    render(<ExportButton filter={CUSTOM_FILTER} latestMonth="2026-09" />);
    await clickExport('JSON');

    const download = anchors.find((a) => a.href.startsWith('blob:'));
    expect(download?.download).toBe('analytics.json');
  });
});
