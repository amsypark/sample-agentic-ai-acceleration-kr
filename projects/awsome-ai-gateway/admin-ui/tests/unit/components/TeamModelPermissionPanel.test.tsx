// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 팀별 허용 모델 패널 — 조회 **실패** 를 "제한 없음 (전체 허용)" 으로 그리지 않는다.
 *
 * boolean `loaded` 하나이던 동안 실패 분기가 `setAllowedAliases([])` + `setLoaded(true)` 였고, 빈 목록은
 * 서버가 "제한 없음" 을 뜻하는 실제 값이라 실패가 "전체 허용" 배지 + 빈 체크박스로 그려졌다. 그 상태에서
 * 운영자가 체크해 저장하면 실제 whitelist 를 모르는 채 덮어쓴다. 서버 액션 RPC 자체가 거부되면
 * (배포 스큐·오프라인) 화면은 영원히 "로딩 중" 이었다.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ModelListItem } from '@/types/entities';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}:${Object.values(values).join(',')}` : key,
}));

const getTeamAllowedModelsAction = vi.fn();
vi.mock('@/lib/actions/models', () => ({
  getTeamAllowedModelsAction: (...a: unknown[]) => getTeamAllowedModelsAction(...a),
  setTeamAllowedModelsAction: vi.fn(),
  clearTeamAllowedModelsAction: vi.fn(),
}));

vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast: vi.fn() }) }));

import { TeamModelPermissionPanel } from '@/components/models/TeamModelPermissionPanel';

const TEAMS = [{ id: 't1', name: 'Platform' }];
const MODELS = [
  { alias: 'claude-x', is_active: true },
  { alias: 'claude-y', is_active: true },
] as unknown as ModelListItem[];

async function selectTeam() {
  const user = userEvent.setup();
  render(<TeamModelPermissionPanel teams={TEAMS} models={MODELS} />);
  await user.selectOptions(screen.getByRole('combobox'), 't1');
  return user;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('TeamModelPermissionPanel load states', () => {
  it('a failed load is "unknown", not "no restriction"', async () => {
    getTeamAllowedModelsAction.mockResolvedValue({ success: false, error: 'HTTP 500' });
    await selectTeam();

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('teamPermissionLoadFailed:HTTP 500'));
    expect(screen.queryByText('noRestriction')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull(); // 편집 UI 는 닫혀 있다
    expect(screen.queryByText('loadingText')).toBeNull();
  });

  it('a rejected server-action RPC also ends in "failed", not an endless spinner', async () => {
    getTeamAllowedModelsAction.mockRejectedValue(new Error('network down'));
    await selectTeam();

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('network down'));
    expect(screen.queryByText('loadingText')).toBeNull();
  });

  it('retry reloads the same team', async () => {
    getTeamAllowedModelsAction
      .mockResolvedValueOnce({ success: false, error: 'HTTP 500' })
      .mockResolvedValueOnce({ success: true, data: { model_aliases: ['claude-x'] } });
    const user = await selectTeam();

    await user.click(await screen.findByRole('button', { name: 'retryLoad' }));

    await waitFor(() => expect(screen.getByText('restrictionApplied')).toBeInTheDocument());
    expect(getTeamAllowedModelsAction).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('checkbox', { name: 'claude-x' })).toBeChecked();
  });

  it('a successful empty list is the real "no restriction" (control)', async () => {
    getTeamAllowedModelsAction.mockResolvedValue({ success: true, data: { model_aliases: [] } });
    await selectTeam();

    await waitFor(() => expect(screen.getByText('noRestriction')).toBeInTheDocument());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);
  });
});
