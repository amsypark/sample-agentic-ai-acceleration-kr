// Copyright 2026 © Amazon.com and Affiliates.

/**
 * AWS 계정 레지스트리 패널 (마이그레이션 0044).
 *
 * 지키는 것: 계정 편집은 그 계정을 고른 **모든** 모델의 청구 계정을 바꾼다. 그래서
 *   - 역할 ARN·ExternalId 를 바꾸는 저장은 영향받는 모델을 보여 주는 확인을 거친다
 *   - 표시 이름만 바꾸는 저장은 확인 없이 간다(확인 피로가 진짜 경고를 묻지 않게)
 *   - 빈 External ID 는 null 로 간다(빈 문자열은 서버가 2자 미만으로 거절한다)
 *   - 이름은 편집할 수 없다(FK 대상)
 *   - 삭제 실패(409: 아직 쓰는 모델이 있음)는 서버 메시지를 그대로 보여 준다
 *   - 앱 단위 계정 목록이 "기본값" 이 무엇인지 설명한다(backend 포함 — 어느 경로에 걸리는지가 다르다)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AppAccount, AwsAccount } from '@/types/entities';
import { AwsAccountSchema } from '@/types/api';

const createAwsAccountAction = vi.fn();
const updateAwsAccountAction = vi.fn();
const deleteAwsAccountAction = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/actions/awsAccounts', () => ({
  createAwsAccountAction: (...a: unknown[]) => createAwsAccountAction(...a),
  updateAwsAccountAction: (...a: unknown[]) => updateAwsAccountAction(...a),
  deleteAwsAccountAction: (...a: unknown[]) => deleteAwsAccountAction(...a),
}));
vi.mock('next-intl', () => ({
  useTranslations: () => (k: string, v?: Record<string, unknown>) =>
    v ? `${k}:${JSON.stringify(v)}` : k,
}));
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

import { AwsAccountsPanel } from '@/components/models/AwsAccountsPanel';

/** 모달의 제출 버튼(그 모달을 연 행의 "edit" 버튼이 아니라). */
function submitEdit() {
  const form = screen.getByLabelText('accountRoleArnLabel').closest('form')!;
  return within(form).getByRole('button', { name: 'edit' });
}

const ROLE = 'arn:aws:iam::111122223333:role/in-region-bedrock';
const USED: AwsAccount = {
  name: 'inregion-seoul',
  display_name: 'In-Region Seoul',
  role_arn: ROLE,
  account_id: '111122223333',
  external_id: 'ext-1',
  description: null,
  aliases: ['claude-sonnet-5-5-kr', 'claude-opus-5-5-kr'],
  app_profiles: [],
};
const APPS: AppAccount[] = [
  {
    client: 'claude-code',
    backend: 'invoke',
    role_arn: 'arn:aws:iam::444455556666:role/claude-code-bedrock',
    account_id: '444455556666',
    registered_as: null,
  },
];

beforeEach(() => {
  for (const f of [createAwsAccountAction, updateAwsAccountAction, deleteAwsAccountAction, toast]) {
    f.mockReset();
  }
  createAwsAccountAction.mockResolvedValue({ success: true, data: USED });
  updateAwsAccountAction.mockResolvedValue({ success: true, data: USED });
  deleteAwsAccountAction.mockResolvedValue({ success: true, data: undefined });
});

describe('AwsAccountsPanel', () => {
  it('lists each account with its account id and how many models use it', () => {
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    expect(screen.getByText('In-Region Seoul')).toBeInTheDocument();
    expect(screen.getByText('111122223333')).toBeInTheDocument();
    expect(screen.getByText('awsAccountAliasCount:{"count":2}')).toBeInTheDocument();
  });

  it('shows the empty state when nothing is registered', () => {
    render(<AwsAccountsPanel accounts={[]} appAccounts={[]} />);
    expect(screen.getByText('awsAccountNone')).toBeInTheDocument();
  });

  it('explains the default with the app-level accounts and their backend', () => {
    render(<AwsAccountsPanel accounts={[]} appAccounts={APPS} />);
    const box = screen.getByTestId('app-accounts');
    expect(box).toHaveTextContent('claude-code');
    expect(box).toHaveTextContent('invoke');
    expect(box).toHaveTextContent('444455556666');
  });

  it('registers an account with a null External ID when the field is empty', async () => {
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'awsAccountAdd' }));
    await user.type(screen.getByLabelText('awsAccountName'), 'inregion-seoul');
    await user.type(screen.getByLabelText('awsAccountDisplayName'), ' In-Region Seoul ');
    await user.type(screen.getByLabelText('accountRoleArnLabel'), ROLE);
    await user.click(screen.getByRole('button', { name: 'register' }));

    await waitFor(() => expect(createAwsAccountAction).toHaveBeenCalled());
    expect(createAwsAccountAction.mock.calls[0][0]).toEqual({
      name: 'inregion-seoul',
      display_name: 'In-Region Seoul',
      role_arn: ROLE,
      external_id: null,
      description: null,
    });
  });

  it('shows the server field errors next to the fields', async () => {
    createAwsAccountAction.mockResolvedValue({
      success: false,
      error: 'Validation failed',
      fieldErrors: { role_arn: 'Must be an IAM role ARN' },
    });
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'awsAccountAdd' }));
    await user.type(screen.getByLabelText('awsAccountName'), 'x-acct');
    await user.type(screen.getByLabelText('awsAccountDisplayName'), 'X');
    await user.type(screen.getByLabelText('accountRoleArnLabel'), 'arn:aws:iam::111122223333:user/u');
    await user.click(screen.getByRole('button', { name: 'register' }));
    expect(await screen.findByText('Must be an IAM role ARN')).toBeInTheDocument();
  });

  it('asks for confirmation before re-pointing the models that use an account', async () => {
    // 청구 변경: 새 역할은 이 계정을 고른 모든 모델을 다른 계정으로 옮긴다. 확인 창이 그 모델들을
    // 나열하고, 수락하기 전에는 아무것도 보내지 않는다.
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'edit' }));
    const arn = screen.getByLabelText('accountRoleArnLabel');
    await user.clear(arn);
    await user.type(arn, 'arn:aws:iam::111122223333:role/in-region-v2');
    await user.click(submitEdit());

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('claude-sonnet-5-5-kr');
    expect(updateAwsAccountAction).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));
    await waitFor(() => expect(updateAwsAccountAction).toHaveBeenCalled());
    expect(updateAwsAccountAction.mock.calls[0][0]).toBe('inregion-seoul');
    expect(updateAwsAccountAction.mock.calls[0][1].role_arn).toBe(
      'arn:aws:iam::111122223333:role/in-region-v2',
    );
  });

  it('treats clearing the External ID as a re-point too', async () => {
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'edit' }));
    await user.clear(screen.getByLabelText('accountExternalIdLabel'));
    await user.click(submitEdit());
    expect(await screen.findByRole('dialog')).toHaveTextContent('claude-opus-5-5-kr');
    expect(updateAwsAccountAction).not.toHaveBeenCalled();
  });

  it('saves a display-name-only edit without the re-point confirmation', async () => {
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'edit' }));
    const dn = screen.getByLabelText('awsAccountDisplayName');
    await user.clear(dn);
    await user.type(dn, 'Seoul (In-Region)');
    await user.click(submitEdit());

    await waitFor(() => expect(updateAwsAccountAction).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does not let the name be edited', async () => {
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'edit' }));
    expect(screen.getByLabelText('awsAccountName')).toBeDisabled();
  });

  it('surfaces the server reason when a delete is refused', async () => {
    deleteAwsAccountAction.mockResolvedValue({
      success: false,
      error:
        "AWS account 'inregion-seoul' is selected by 2 alias(es): claude-opus-5-5-kr, claude-sonnet-5-5-kr.",
    });
    const user = userEvent.setup();
    render(<AwsAccountsPanel accounts={[USED]} appAccounts={[]} />);
    await user.click(screen.getByRole('button', { name: 'awsAccountDelete' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /confirm/i }));

    await waitFor(() => expect(toast).toHaveBeenCalled());
    const t0 = toast.mock.calls[0][0];
    expect(t0.type).toBe('error');
    expect(t0.message).toContain('claude-sonnet-5-5-kr');
  });
});

describe('AwsAccountSchema — mirrors the admin-api / DB rules', () => {
  const base = { name: 'inregion-seoul', display_name: 'S', role_arn: ROLE };

  it.each(['default', 'gateway', 'inherit', 'none', 'ab', '-x1', 'x1-', 'Upper', 'under_score'])(
    'refuses the name %s',
    (name) => {
      expect(AwsAccountSchema.safeParse({ ...base, name }).success).toBe(false);
    },
  );

  it.each([
    'arn:aws:iam::111122223333:user/someone',
    '111122223333',
    'arn:aws:iam::12345:role/x',
    'arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude',
  ])('refuses the non-role ARN %s', (role_arn) => {
    expect(AwsAccountSchema.safeParse({ ...base, role_arn }).success).toBe(false);
  });

  it('accepts other partitions, role paths and a null External ID', () => {
    expect(
      AwsAccountSchema.safeParse({
        ...base,
        role_arn: 'arn:aws-us-gov:iam::111122223333:role/path/r',
        external_id: null,
      }).success,
    ).toBe(true);
  });

  it.each(['x', 'has space'])('refuses the External ID %s', (external_id) => {
    expect(AwsAccountSchema.safeParse({ ...base, external_id }).success).toBe(false);
  });
});
