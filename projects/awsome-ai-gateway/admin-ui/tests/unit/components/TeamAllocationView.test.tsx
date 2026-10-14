// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * TeamAllocationView — 팀 리더의 예산 배정 화면.
 *
 * 결함 두 개가 겹쳐 이 화면은 저장이 불가능했다:
 *   1. `*_usd` 는 pydantic Decimal = JSON **문자열**("100.0000")인데 number 로 선언돼
 *      `totalBudget.toFixed(2)` 가 TypeError 로 렌더를 죽였다(`.toFixed is not a function`).
 *      reduce 합계는 문자열 연결이 됐다.
 *   2. GET /allocation 의 첫 항목은 **팀 합계 행**(target_type=TEAM, allocated = 팀 총예산)인데
 *      그대로 멤버 표에 그려지고 합계에 더해져, 멤버에게 1달러만 배정해도 '초과' 로 저장이 막혔다.
 *
 * 와이어 모양은 admin-api 가 직렬화한 실제 형태를 따른다(tests/integration/test_budget_wire_contract.py).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { TeamBudgetAllocation } from '@/types/entities';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}:${Object.values(values).join(',')}` : key,
}));

const allocateTeamBudgetAction = vi.fn();
vi.mock('@/lib/actions/budgets', () => ({
  allocateTeamBudgetAction: (...a: unknown[]) => allocateTeamBudgetAction(...a),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

import { TeamAllocationView } from '@/components/budgets/TeamAllocationView';

const TEAM_ID = '11111111-1111-1111-1111-111111111111';

// admin-api 가 실제로 보내는 모양 — Decimal 은 문자열, 첫 항목은 팀 합계 행.
const WIRE: TeamBudgetAllocation = {
  team_id: TEAM_ID,
  team_name: 'Platform',
  total_budget_usd: '100.0000',
  entries: [
    {
      target_id: TEAM_ID,
      target_name: 'Platform',
      target_type: 'TEAM',
      allocated_usd: '100.0000',
      used_usd: '12.5000',
      remaining_usd: '87.5000',
      alert_level: 'NORMAL',
    },
    {
      target_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      target_name: 'Alice',
      target_type: 'USER',
      allocated_usd: '60.0000',
      used_usd: '10.0000',
      remaining_usd: '50.0000',
      alert_level: 'NORMAL',
    },
    {
      target_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      target_name: 'Bob',
      target_type: 'USER',
      allocated_usd: '0.0000',
      used_usd: '0.0000',
      remaining_usd: '0.0000',
      alert_level: 'NORMAL',
    },
  ],
} as unknown as TeamBudgetAllocation;

beforeEach(() => {
  vi.clearAllMocks();
  allocateTeamBudgetAction.mockResolvedValue({ success: true, data: undefined });
});

describe('TeamAllocationView', () => {
  it('Decimal 문자열 와이어로 렌더된다 (예전: toFixed TypeError)', () => {
    render(<TeamAllocationView teamId={TEAM_ID} initialAllocation={WIRE} />);
    expect(screen.getByText('$100.00')).toBeInTheDocument(); // 팀 총예산
    expect(screen.getByText('$10.00')).toBeInTheDocument(); // Alice 사용액
    // 미배정 = 100 − (60 + 0) — 팀 합계 행(100)이 더해지면 -60 이 된다.
    expect(screen.getByText('$40.00')).toBeInTheDocument();
  });

  it('팀 합계 행을 멤버로 그리지 않는다', () => {
    render(<TeamAllocationView teamId={TEAM_ID} initialAllocation={WIRE} />);
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();
    // 'Platform' 은 헤더(teamTotalBudget:Platform)에만 있고 멤버 행에는 없다.
    expect(screen.queryByText('Platform')).toBeNull();
    expect(screen.getAllByRole('spinbutton')).toHaveLength(2);
  });

  it('배정이 총예산 안이면 저장이 열려 있고 USER 행만 넘긴다', async () => {
    const user = userEvent.setup();
    render(<TeamAllocationView teamId={TEAM_ID} initialAllocation={WIRE} />);

    expect(screen.queryByRole('alert')).toBeNull(); // 초과 경고 없음
    const save = screen.getByRole('button', { name: 'save' });
    expect(save).not.toBeDisabled();

    await user.click(save);
    await waitFor(() => expect(allocateTeamBudgetAction).toHaveBeenCalledTimes(1));
    const [teamId, rows] = allocateTeamBudgetAction.mock.calls[0];
    expect(teamId).toBe(TEAM_ID);
    expect(rows.map((r: { target_type: string }) => r.target_type)).toEqual(['USER', 'USER']);
  });

  it('편집해서 총예산을 넘기면 저장을 막는다 (대조군 — 초과 판정 자체는 살아 있다)', async () => {
    const user = userEvent.setup();
    render(<TeamAllocationView teamId={TEAM_ID} initialAllocation={WIRE} />);

    const [, bobInput] = screen.getAllByRole('spinbutton');
    await user.clear(bobInput);
    await user.type(bobInput, '50'); // 60 + 50 = 110 > 100

    expect(screen.getByRole('alert')).toHaveTextContent('allocationExceeds:110.00,100.00');
    expect(screen.getByRole('button', { name: 'save' })).toBeDisabled();
  });
});
