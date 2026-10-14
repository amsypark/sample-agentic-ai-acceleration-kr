// Copyright 2026 © Amazon.com and Affiliates.

/**
 * 모델 다이얼로그의 대상 AWS 계정 칸(0044)과 모델 표의 계정 열.
 *
 * 실패 모드는 "성공처럼 보이는 저장" 이다:
 *   - 편집 다이얼로그가 현재 선택으로 prefill 하지 않으면, 계정 칸을 건드리지 않은 저장이 선택을
 *     지운다(update 는 account_name 을 항상 보낸다 — null 은 해제다);
 *   - 목록에 없는 현재 값(조회 실패 등)을 select 가 첫 선택지(기본값)로 보여 주면 같은 일이 생긴다;
 *   - OPENMODEL 모델에 계정을 보내면 서버가 400 으로 막지만, 애초에 고를 수 없어야 한다.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import type { AwsAccount, ModelListItem } from '@/types/entities';

const createModelAction = vi.fn();
const updateModelAction = vi.fn();
const activateModelAction = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/actions/models', () => ({
  createModelAction: (...a: unknown[]) => createModelAction(...a),
  updateModelAction: (...a: unknown[]) => updateModelAction(...a),
  activateModelAction: (...a: unknown[]) => activateModelAction(...a),
  deactivateModelAction: vi.fn(),
}));
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

import { CreateModelDialog } from '@/components/models/CreateModelDialog';
import { ModelsTable } from '@/components/models/ModelsTable';

const ACCOUNTS: AwsAccount[] = [
  {
    name: 'inregion-seoul',
    display_name: 'In-Region Seoul',
    role_arn: 'arn:aws:iam::111122223333:role/in-region-bedrock',
    account_id: '111122223333',
    external_id: null,
    description: null,
    aliases: ['claude-x'],
    app_profiles: [],
  },
];

function model(over: Partial<ModelListItem> = {}): ModelListItem {
  return {
    alias: 'claude-x',
    provider: 'BEDROCK',
    model_id: 'global.anthropic.claude-x',
    endpoint_url: null,
    is_active: true,
    input_price_per_1k: 0.001,
    output_price_per_1k: 0.002,
    cache_creation_5m_price_per_1k: 0,
    cache_creation_1h_price_per_1k: 0,
    cache_read_price_per_1k: 0,
    max_tokens: 0,
    context_window: 0,
    description: null,
    display_name: null,
    account_name: null,
    account_role_arn: null,
    ...over,
  };
}

function select() {
  return screen.getByLabelText('accountSelectLabel') as HTMLSelectElement;
}

function submit() {
  fireEvent.submit(select().closest('form')!);
}

beforeEach(() => {
  for (const f of [createModelAction, updateModelAction, activateModelAction, toast]) f.mockReset();
  createModelAction.mockResolvedValue({ success: true, data: undefined });
  updateModelAction.mockResolvedValue({ success: true, data: undefined });
});

describe('CreateModelDialog — target AWS account', () => {
  it('offers Default plus every registered account', () => {
    render(<CreateModelDialog isOpen onClose={() => {}} accounts={ACCOUNTS} />);
    const opts = Array.from(select().options).map((o) => [o.value, o.textContent]);
    expect(opts).toEqual([
      ['', 'accountDefaultOption'],
      ['inregion-seoul', 'In-Region Seoul (111122223333) — inregion-seoul'],
    ]);
  });

  it('says how to register one when the registry is empty', () => {
    render(<CreateModelDialog isOpen onClose={() => {}} accounts={[]} />);
    expect(screen.getByText('accountNoneRegistered')).toBeInTheDocument();
  });

  it('sends the selected account name, and null for Default, on create', async () => {
    render(<CreateModelDialog isOpen onClose={() => {}} accounts={ACCOUNTS} />);
    fireEvent.change(screen.getByLabelText(/Alias/), { target: { name: 'alias', value: 'claude-x' } });
    fireEvent.change(screen.getByLabelText(/Provider/), { target: { name: 'provider', value: 'BEDROCK' } });
    fireEvent.change(screen.getByLabelText(/Model ID/), {
      target: { name: 'model_id', value: 'global.anthropic.claude-x' },
    });
    fireEvent.change(screen.getByLabelText('priceInput'), { target: { name: 'input_price_per_1k', value: '0.001' } });
    fireEvent.change(screen.getByLabelText('priceOutput'), { target: { name: 'output_price_per_1k', value: '0.002' } });
    fireEvent.change(select(), { target: { name: 'account_name', value: 'inregion-seoul' } });
    submit();
    await waitFor(() => expect(createModelAction).toHaveBeenCalled());
    expect(createModelAction.mock.calls[0][0].account_name).toBe('inregion-seoul');

    fireEvent.change(select(), { target: { name: 'account_name', value: '' } });
    submit();
    await waitFor(() => expect(createModelAction).toHaveBeenCalledTimes(2));
    expect(createModelAction.mock.calls[1][0].account_name).toBeNull();
  });

  it('prefills the current selection when editing, so an untouched save keeps it', async () => {
    render(
      <CreateModelDialog
        isOpen
        onClose={() => {}}
        accounts={ACCOUNTS}
        editModel={model({ account_name: 'inregion-seoul' })}
      />,
    );
    expect(select().value).toBe('inregion-seoul');
    submit();
    await waitFor(() => expect(updateModelAction).toHaveBeenCalled());
    expect(updateModelAction.mock.calls[0][1].account_name).toBe('inregion-seoul');
  });

  it('clears the selection with null when Default is picked on edit', async () => {
    render(
      <CreateModelDialog
        isOpen
        onClose={() => {}}
        accounts={ACCOUNTS}
        editModel={model({ account_name: 'inregion-seoul' })}
      />,
    );
    fireEvent.change(select(), { target: { name: 'account_name', value: '' } });
    submit();
    await waitFor(() => expect(updateModelAction).toHaveBeenCalled());
    expect(updateModelAction.mock.calls[0][1].account_name).toBeNull();
  });

  it('keeps a current account that is missing from the list instead of silently clearing it', async () => {
    render(
      <CreateModelDialog
        isOpen
        onClose={() => {}}
        accounts={[]}
        editModel={model({ account_name: 'unlisted-acct' })}
      />,
    );
    expect(select().value).toBe('unlisted-acct');
    submit();
    await waitFor(() => expect(updateModelAction).toHaveBeenCalled());
    expect(updateModelAction.mock.calls[0][1].account_name).toBe('unlisted-acct');
  });

  it('does not offer an account for an OPENMODEL model and always sends null', async () => {
    render(
      <CreateModelDialog
        isOpen
        onClose={() => {}}
        accounts={ACCOUNTS}
        editModel={model({ provider: 'OPENMODEL', model_id: 'meta-llama/x', account_name: null })}
      />,
    );
    expect(select()).toBeDisabled();
    expect(screen.getByText('accountOpenmodelNote')).toBeInTheDocument();
    submit();
    await waitFor(() => expect(updateModelAction).toHaveBeenCalled());
    expect(updateModelAction.mock.calls[0][1].account_name).toBeNull();
  });
});

describe('ModelsTable — AWS account column', () => {
  it('shows Default (with its meaning) for a model without an account', () => {
    render(<ModelsTable models={[model()]} accounts={ACCOUNTS} />);
    const cell = screen.getByText('accountColumnDefault');
    expect(cell).toHaveAttribute('title', 'accountColumnDefaultTitle');
  });

  it('shows the account display name and the account id the gateway signs with', () => {
    render(
      <ModelsTable
        models={[
          model({
            account_name: 'inregion-seoul',
            account_role_arn: 'arn:aws:iam::111122223333:role/in-region-bedrock',
          }),
        ]}
        accounts={ACCOUNTS}
      />,
    );
    expect(screen.getByText('In-Region Seoul')).toBeInTheDocument();
    expect(screen.getByText('111122223333')).toBeInTheDocument();
  });
});
