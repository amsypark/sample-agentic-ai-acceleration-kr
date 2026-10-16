// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Cognito 동기화 토스트 — 서버가 보내는 `teams_deleted` 를 버리지 않는다.
 *
 * admin-api 는 여섯 번째 카운터 `teams_deleted` 를 늘 보냈다(이름과 달리 팀은 삭제되지 않는다 —
 * Cognito 그룹이 사라진 팀의 활성 멤버가 **기본 팀으로 이동**한 팀 수다). 액션 타입에 이 필드가
 * 없어 화면까지 오지 못했고, 동기화가 N개 팀의 예산·허용 모델·레이트리밋 축을 바꿔도 토스트는
 * 초록 "성공" 만 말했다.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const syncCognitoAction = vi.fn();
vi.mock('@/lib/actions/users', () => ({
  syncCognitoAction: (...a: unknown[]) => syncCognitoAction(...a),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

import { CognitoSyncButton } from '@/components/users/CognitoSyncButton';

const SUMMARY = {
  groups_synced: 3,
  users_created: 0,
  users_updated: 1,
  users_deactivated: 0,
  teams_deleted: 0,
  errors: [] as string[],
};

async function runSync() {
  const user = userEvent.setup();
  render(<CognitoSyncButton />);
  await user.click(screen.getByRole('button', { name: /Cognito 동기화/ }));
  await user.click(screen.getByRole('button', { name: '동기화 실행' }));
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  return toast.mock.calls[0][0] as { type: string; message: string };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CognitoSyncButton', () => {
  it('reports teams whose members were moved to the default team, as a warning', async () => {
    syncCognitoAction.mockResolvedValue({ success: true, data: { ...SUMMARY, teams_deleted: 2 } });
    const shown = await runSync();
    expect(shown.type).toBe('warning');
    expect(shown.message).toContain('팀 2개의 멤버를 기본 팀으로 이동');
  });

  it('stays a plain success when no team was reassigned (control)', async () => {
    syncCognitoAction.mockResolvedValue({ success: true, data: SUMMARY });
    const shown = await runSync();
    expect(shown.type).toBe('success');
    expect(shown.message).not.toContain('기본 팀');
    expect(shown.message).toContain('그룹 3개 동기화');
  });
});
