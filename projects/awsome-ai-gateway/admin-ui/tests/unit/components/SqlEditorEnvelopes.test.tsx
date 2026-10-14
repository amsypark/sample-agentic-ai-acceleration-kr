// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 인라인 SQL 에디터(ToolCallBlock 안) — 실패 봉투를 **성공으로 그리지 않는다**.
 *
 * 예전 코드는 `setResult(await r.json())` 후 `result.ok === false` 하나로만 에러를 판정했다.
 * `r.ok` 는 보지 않았다. 그래서 아래 봉투가 전부 **성공 분기**로 렌더됐다:
 *
 *   200 `{errorMessage, errorType, stackTrace}`  ← Lambda 크래시/타임아웃(admin-api 가
 *                                                  FunctionError 를 검사하지 않았다)
 *   502 `{detail: "..."}`                        ← Lambda 호출 실패
 *   403 `{detail: "..."}`                        ← 세션 소유
 *   500 `{error: {message}}`                     ← 이 앱의 에러 봉투
 *
 * 성공 분기의 표시는 "재실행 결과" + "0행" 이다 — 크래시가 **권위 있는 빈 결과셋**으로 보였다.
 * 그래서 두 가지를 짝으로 고정한다: (a) 실패 봉투 전부 에러 배너 + "0행" 없음,
 * (b) **진짜 0행 성공은 여전히 "0행"** — 이 대조군이 없으면 "다 에러로 처리" 라는 반대 방향
 * 회귀를 못 막는다. 예전 코드가 붕괴시킨 것이 정확히 이 두 상태의 구분이다.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, afterEach } from 'vitest';

// ChartRenderer 는 recharts 를 끌어오므로 jsdom 에서 무겁다 — 표 렌더 여부만 알면 된다.
vi.mock('@/components/chat/ChartRenderer', () => ({
  ChartRenderer: () => <div data-testid="chart-renderer" />,
}));

import { ToolCallBlock } from '@/components/chat/ToolCallBlock';

function renderEditor() {
  return render(
    <ToolCallBlock
      toolCall={{ tool: 'ask_sql_specialist', result: { sql: 'SELECT 1' }, status: 'done' }}
      sessionId="11111111-1111-4111-8111-111111111111"
    />,
  );
}

async function runQuery(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /SQL Specialist 호출/ }));
  await user.click(screen.getByRole('button', { name: /실행/ }));
}

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: status >= 200 && status < 300, status, json: async () => body }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('failure envelopes never render as a successful empty result', () => {
  const cases: Array<[string, number, unknown, string]> = [
    [
      'lambda crash payload at 200',
      200,
      { errorMessage: 'Task timed out after 30.00 seconds', errorType: 'Sandbox.Timedout' },
      'Task timed out after 30.00 seconds',
    ],
    ['normalized crash (ok:false) at 200', 200, { ok: false, error: 'Lambda Unhandled' }, 'Lambda Unhandled'],
    ['invoke failure at 502', 502, { detail: 'query execution failed: AccessDenied' }, 'AccessDenied'],
    ['forbidden session at 403', 403, { detail: 'Forbidden' }, 'Forbidden'],
    ['app error envelope at 500', 500, { error: { type: 'internal_error', message: 'boom', code: 'X' } }, 'boom'],
    ['unknown body at 200 (no ok flag)', 200, { rows: [] }, '실행 실패'],
  ];

  it.each(cases)('%s shows the error banner', async (_name, status, body, expected) => {
    mockFetch(status, body);
    const user = userEvent.setup();
    renderEditor();
    await runQuery(user);

    await waitFor(() => expect(screen.getByText(new RegExp(expected))).toBeInTheDocument());
    // "0행" 이 보이면 크래시가 빈 결과셋으로 위장된 것이다.
    expect(screen.queryByText('0행')).toBeNull();
    expect(screen.queryByText(/재실행 결과/)).toBeNull();
  });
});

describe('successes still render as successes (controls)', () => {
  it('a genuine zero-row success still says 0 rows', async () => {
    mockFetch(200, { ok: true, rows: [], row_count: 0 });
    const user = userEvent.setup();
    renderEditor();
    await runQuery(user);

    await waitFor(() => expect(screen.getByText('0행')).toBeInTheDocument());
    expect(screen.getByText(/재실행 결과/)).toBeInTheDocument();
  });

  it('rows render through the table renderer', async () => {
    mockFetch(200, { ok: true, rows: [{ n: 1 }], row_count: 1 });
    const user = userEvent.setup();
    renderEditor();
    await runQuery(user);

    await waitFor(() => expect(screen.getByTestId('chart-renderer')).toBeInTheDocument());
    expect(screen.getByText(/\(1행\)/)).toBeInTheDocument();
  });
});
