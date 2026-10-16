'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AWS 계정 레지스트리 패널 (마이그레이션 0044).
 *
 * 계정을 한 번 등록하고 모델 편집 다이얼로그에서 이름으로 고른다. 이 패널이 보여 주는 것:
 *   - 등록된 계정: 표시 이름, 계정 번호, 역할, ExternalId 설정 여부, **어떤 모델이 쓰는지**
 *   - 앱 단위 계정(라우팅 프로필): 모델의 계정을 비워 두면 그 경로가 앱 계정을 쓸 때 여기 계정으로
 *     나간다 — 표의 "기본값" 이 무엇인지 운영자가 알 수 있게.
 *
 * ★ 편집이 위험한 이유를 화면에서 말한다: 역할 ARN·ExternalId 를 고치면 이 계정을 고른 모든 모델의
 *   호출·청구 계정이 함께 바뀐다. 그래서 그런 저장은 영향받는 모델을 나열한 확인을 거친다. 삭제는
 *   모델이 남아 있으면 admin-api 가 409 로 막는다.
 */

import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import { X } from 'lucide-react';
import type { AppAccount, AwsAccount } from '@/types/entities';
import {
  createAwsAccountAction,
  deleteAwsAccountAction,
  updateAwsAccountAction,
} from '@/lib/actions/awsAccounts';
import { useToast } from '@/components/common/ToastProvider';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';

interface AwsAccountsPanelProps {
  accounts: AwsAccount[];
  appAccounts: AppAccount[];
}

interface FormState {
  name: string;
  display_name: string;
  role_arn: string;
  external_id: string;
  description: string;
}

const EMPTY: FormState = { name: '', display_name: '', role_arn: '', external_id: '', description: '' };

const INPUT =
  'w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60';

/** `arn:aws:iam::<id>:role/<path/name>` → `<path/name>`(표에 맞게 줄인다; 전체는 title 로). */
function roleName(arn: string): string {
  return arn.split('/').slice(1).join('/');
}

export function AwsAccountsPanel({ accounts, appAccounts }: AwsAccountsPanelProps) {
  const t = useTranslations('models');
  const tc = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [editing, setEditing] = useState<AwsAccount | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [pendingRepoint, setPendingRepoint] = useState<FormState | null>(null);
  const [pendingDelete, setPendingDelete] = useState<AwsAccount | null>(null);

  const open = (a: AwsAccount | null) => {
    setEditing(a);
    setForm(
      a
        ? {
            name: a.name,
            display_name: a.display_name,
            role_arn: a.role_arn,
            external_id: a.external_id ?? '',
            description: a.description ?? '',
          }
        : EMPTY,
    );
    setFieldErrors({});
    setError(null);
    setIsOpen(true);
  };

  const close = () => {
    setIsOpen(false);
    setEditing(null);
  };

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setForm((p) => ({ ...p, [name]: value }));
    setFieldErrors((p) => {
      if (!p[name]) return p;
      const n = { ...p };
      delete n[name];
      return n;
    });
  };

  const save = (f: FormState) => {
    const payload = {
      name: f.name.trim(),
      display_name: f.display_name.trim(),
      role_arn: f.role_arn.trim(),
      // 빈 칸 = 없음(null). 빈 문자열을 보내면 2자 미만이라 거절된다.
      external_id: f.external_id.trim() || null,
      description: f.description.trim() || null,
    };
    startTransition(async () => {
      const result = editing
        ? await updateAwsAccountAction(editing.name, payload)
        : await createAwsAccountAction(payload);
      if (result.success) {
        toast({
          type: 'success',
          message: editing ? t('awsAccountUpdated') : t('awsAccountCreated'),
          auto_dismiss_ms: 3000,
        });
        close();
      } else {
        setFieldErrors(result.fieldErrors ?? {});
        setError(result.error);
      }
    });
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // 계정의 정체성(역할·ExternalId)을 바꾸는 저장이고 이 계정을 고른 모델이 있으면, 그 모델들의
    // 청구 계정이 함께 바뀐다 — 확인 없이 통과시키지 않는다.
    const identityChanged =
      !!editing &&
      (form.role_arn.trim() !== editing.role_arn ||
        (form.external_id.trim() || null) !== (editing.external_id ?? null));
    if (identityChanged && editing!.aliases.length > 0) {
      setPendingRepoint(form);
      return;
    }
    save(form);
  };

  const onDelete = (a: AwsAccount) => {
    startTransition(async () => {
      const result = await deleteAwsAccountAction(a.name);
      if (result.success) {
        toast({ type: 'success', message: t('awsAccountDeleted'), auto_dismiss_ms: 3000 });
      } else {
        // 409 = 아직 이 계정을 고른 모델이 있다 — admin-api 메시지가 그 모델들을 나열한다.
        toast({
          type: 'error',
          message: `${t('awsAccountDeleteFailed')}: ${result.error}`,
          auto_dismiss_ms: 8000,
        });
      }
      setPendingDelete(null);
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{t('awsAccountsIntro')}</p>
        <button
          type="button"
          onClick={() => open(null)}
          className="inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium bg-primary text-primary-foreground shadow hover:bg-primary/90"
        >
          {t('awsAccountAdd')}
        </button>
      </div>

      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('awsAccountName')}</Th>
              <Th>{t('awsAccountDisplayName')}</Th>
              <Th>{t('awsAccountId')}</Th>
              <Th>{t('accountRoleArnLabel')}</Th>
              <Th>{t('awsAccountUsedBy')}</Th>
              <Th>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {accounts.length === 0 ? (
              <TEmpty colSpan={6}>{t('awsAccountNone')}</TEmpty>
            ) : (
              accounts.map((a) => (
                <Tr key={a.name}>
                  <Td className="font-mono text-xs">{a.name}</Td>
                  <Td>{a.display_name}</Td>
                  <Td className="font-mono text-xs">{a.account_id}</Td>
                  <Td className="font-mono text-xs max-w-[18rem] truncate" title={a.role_arn}>
                    {roleName(a.role_arn)}
                    {a.external_id ? ' · ext' : ''}
                  </Td>
                  <Td>
                    <span title={a.aliases.join(', ')}>
                      {a.aliases.length === 0
                        ? t('awsAccountUnused')
                        : t('awsAccountAliasCount', { count: a.aliases.length })}
                    </span>
                    {a.app_profiles.length > 0 && (
                      <span className="block text-xs text-muted-foreground">
                        {t('awsAccountAlsoApp', { apps: a.app_profiles.join(', ') })}
                      </span>
                    )}
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => open(a)}
                        className="rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent"
                      >
                        {t('edit')}
                      </button>
                      <button
                        type="button"
                        onClick={() => setPendingDelete(a)}
                        className="rounded-md border border-destructive/40 text-destructive px-3 py-1.5 text-xs font-medium hover:bg-destructive/10"
                      >
                        {t('awsAccountDelete')}
                      </button>
                    </div>
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
        </Table>
      </div>

      {appAccounts.length > 0 && (
        <div className="text-xs text-muted-foreground space-y-1" data-testid="app-accounts">
          <p className="font-medium text-foreground">{t('appAccountsTitle')}</p>
          <p>{t('appAccountsNote')}</p>
          <ul className="list-disc pl-5">
            {appAccounts.map((p) => (
              <li key={p.client}>
                <span className="font-medium">{p.client}</span>
                {' · '}
                <span>{p.backend}</span>
                {' · '}
                <span className="font-mono">{p.account_id}</span>
                {' · '}
                <span className="font-mono" title={p.role_arn}>
                  {roleName(p.role_arn)}
                </span>
                {p.registered_as && <> {t('appAccountRegisteredAs', { name: p.registered_as })}</>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {isOpen && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-background rounded-lg p-6 w-full max-w-lg shadow-xl border border-border max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">
                {editing ? t('awsAccountEditTitle') : t('awsAccountCreateTitle')}
              </h2>
              <button type="button" onClick={close} aria-label={tc('close')} className="opacity-70 hover:opacity-100">
                <X size={16} aria-hidden="true" />
              </button>
            </div>
            <form onSubmit={onSubmit} className="space-y-4">
              <p className="text-xs text-muted-foreground">{t('accountSectionNote')}</p>
              <div className="space-y-1">
                <label htmlFor="acct_name" className="text-sm font-medium">{t('awsAccountName')}</label>
                <input
                  id="acct_name"
                  name="name"
                  value={form.name}
                  onChange={onChange}
                  disabled={!!editing}
                  maxLength={64}
                  required
                  className={INPUT}
                  placeholder="inregion-seoul"
                />
                <p className="text-xs text-muted-foreground">{t('awsAccountNameNote')}</p>
                {fieldErrors.name && <FormError error={fieldErrors.name} />}
              </div>
              <div className="space-y-1">
                <label htmlFor="acct_display_name" className="text-sm font-medium">{t('awsAccountDisplayName')}</label>
                <input
                  id="acct_display_name"
                  name="display_name"
                  value={form.display_name}
                  onChange={onChange}
                  maxLength={128}
                  required
                  className={INPUT}
                />
                {fieldErrors.display_name && <FormError error={fieldErrors.display_name} />}
              </div>
              <div className="space-y-1">
                <label htmlFor="acct_role_arn" className="text-sm font-medium">{t('accountRoleArnLabel')}</label>
                <input
                  id="acct_role_arn"
                  name="role_arn"
                  value={form.role_arn}
                  onChange={onChange}
                  maxLength={2048}
                  required
                  className={INPUT}
                  placeholder="arn:aws:iam::111122223333:role/llm-gateway-bedrock"
                />
                <p className="text-xs text-muted-foreground">{t('accountRoleArnNote')}</p>
                {fieldErrors.role_arn && <FormError error={fieldErrors.role_arn} />}
              </div>
              <div className="space-y-1">
                <label htmlFor="acct_external_id" className="text-sm font-medium">{t('accountExternalIdLabel')}</label>
                <input
                  id="acct_external_id"
                  name="external_id"
                  value={form.external_id}
                  onChange={onChange}
                  maxLength={1224}
                  className={INPUT}
                />
                <p className="text-xs text-muted-foreground">{t('accountExternalIdNote')}</p>
                {fieldErrors.external_id && <FormError error={fieldErrors.external_id} />}
              </div>
              <div className="space-y-1">
                <label htmlFor="acct_description" className="text-sm font-medium">{t('awsAccountDescription')}</label>
                <input
                  id="acct_description"
                  name="description"
                  value={form.description}
                  onChange={onChange}
                  maxLength={1000}
                  className={INPUT}
                />
              </div>
              {editing && editing.aliases.length > 0 && (
                <p className="text-xs text-amber-600">
                  {t('awsAccountEditWarning', { aliases: editing.aliases.join(', ') })}
                </p>
              )}
              {error && <FormError error={error} />}
              <div className="flex justify-end gap-2">
                <button type="button" onClick={close} className="rounded-md border border-border px-4 py-2 text-sm">
                  {tc('cancel')}
                </button>
                <SpinnerButton type="submit" isLoading={isPending}>
                  {editing ? t('edit') : t('register')}
                </SpinnerButton>
              </div>
            </form>
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={!!pendingRepoint}
        onClose={() => setPendingRepoint(null)}
        onConfirm={() => {
          const f = pendingRepoint!;
          setPendingRepoint(null);
          save(f);
        }}
        title={t('awsAccountRepointTitle')}
        message={t('awsAccountRepointMessage', { aliases: editing?.aliases.join(', ') ?? '' })}
        isDestructive
      />
      <ConfirmDialog
        isOpen={!!pendingDelete}
        onClose={() => setPendingDelete(null)}
        onConfirm={() => pendingDelete && onDelete(pendingDelete)}
        title={t('awsAccountDeleteTitle')}
        message={t('awsAccountDeleteMessage', { name: pendingDelete?.name ?? '' })}
        isDestructive
      />
    </div>
  );
}
