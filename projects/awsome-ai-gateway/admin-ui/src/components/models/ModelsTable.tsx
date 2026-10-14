'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { AwsAccount, ModelListItem } from '@/types/entities';
import { activateModelAction } from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { CreateModelDialog } from './CreateModelDialog';
import { DeactivateModelDialog } from './DeactivateModelDialog';

interface ModelsTableProps {
  models: ModelListItem[];
  /** 등록된 AWS 계정(0044) — 계정 열 표시와 편집 다이얼로그의 선택지. */
  accounts?: AwsAccount[];
}

/** 모델의 대상 계정 칸. 선택이 없으면 "기본값" 과 그 뜻을 툴팁으로. */
function AccountCell({ model, accounts }: { model: ModelListItem; accounts: AwsAccount[] }) {
  const t = useTranslations('models');
  if (!model.account_name) {
    return (
      <span className="text-muted-foreground" title={t('accountColumnDefaultTitle')}>
        {t('accountColumnDefault')}
      </span>
    );
  }
  const acct = accounts.find((a) => a.name === model.account_name);
  // 계정 번호는 게이트웨이가 실제로 서명에 쓰는 사본(account_role_arn)에서 읽는다.
  const accountId = model.account_role_arn?.split(':')[4] ?? acct?.account_id ?? '';
  return (
    <span title={model.account_role_arn ?? undefined}>
      {acct?.display_name ?? model.account_name}
      {accountId && <span className="block text-xs text-muted-foreground font-mono">{accountId}</span>}
    </span>
  );
}

function ProviderBadge({ provider }: { provider: string }) {
  const toneMap: Record<string, BadgeTone> = {
    bedrock: 'sky',
    'on-prem': 'pink',
    bedrock_mantle: 'amber',       // Cowork → Mantle Opus (Tokyo)
    bedrock_mantle_openai: 'teal', // Codex → Mantle GPT-5.5 (Ohio)
  };
  return <Badge tone={toneMap[provider.toLowerCase()] ?? 'neutral'}>{provider}</Badge>;
}

function StatusBadge({ isActive, activeLabel, inactiveLabel }: { isActive: boolean; activeLabel: string; inactiveLabel: string }) {
  return <Badge tone={isActive ? 'teal' : 'neutral'}>{isActive ? activeLabel : inactiveLabel}</Badge>;
}

function formatNumber(n: number): string {
  return new Intl.NumberFormat('ko-KR').format(n);
}

/**
 * per-1K 단가 표시. 저장 정밀도(NUMERIC(12,8), 소수 8자리)까지 보여주고 끝자리 0 은 자른다.
 * 최소 4자리는 유지해 열 정렬을 맞춘다.
 *
 * 예전에는 toFixed(4)/toFixed(5) 로 잘라 0.0001375 가 $0.00014 로 보였다 — 청구는 정확한데
 * 화면이 다른 값을 보여 줘서, 화면을 보고 단가를 검수하면 틀린 값을 맞다고 판단하게 된다.
 */
export function formatPer1k(n: number): string {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 4,
    maximumFractionDigits: 8,
    useGrouping: false,
  });
}

export function ModelsTable({ models, accounts = [] }: ModelsTableProps) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [selectedModel, setSelectedModel] = useState<ModelListItem | null>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deactivateDialogOpen, setDeactivateDialogOpen] = useState(false);

  const handleEdit = (model: ModelListItem) => {
    setSelectedModel(model);
    setEditDialogOpen(true);
  };

  const handleDeactivate = (model: ModelListItem) => {
    setSelectedModel(model);
    setDeactivateDialogOpen(true);
  };

  const handleActivate = (model: ModelListItem) => {
    startTransition(async () => {
      const result = await activateModelAction(model.alias);
      if (result.success) {
        toast({
          type: 'success',
          message: t('activateSuccess', { alias: model.alias }),
          auto_dismiss_ms: 3000,
        });
      } else {
        toast({
          type: 'error',
          message: result.error,
          auto_dismiss_ms: 5000,
        });
      }
    });
  };

  return (
    <>
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>Alias</Th>
              <Th>{t('displayName')}</Th>
              <Th>Provider</Th>
              <Th>Model ID</Th>
              <Th numeric>{t('inputPriceShort')}</Th>
              <Th numeric>{t('outputPriceShort')}</Th>
              <Th numeric>{t('cache5min')}</Th>
              <Th numeric>{t('cache1h')}</Th>
              <Th numeric>{t('cacheRead')}</Th>
              <Th>{t('accountColumnHeader')}</Th>
              <Th>{t('status')}</Th>
              <Th>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {models.length === 0 ? (
              <TEmpty colSpan={12}>{t('noModels')}</TEmpty>
            ) : (
              models.map((model) => (
                <Tr key={model.alias}>
                  <Td
                    emphasis
                    className={`font-mono mono-id text-xs${!model.is_active ? ' text-muted-foreground' : ''}`}
                  >
                    {model.alias}
                  </Td>
                  <Td className={!model.is_active ? 'text-muted-foreground' : ''}>
                    {model.display_name ?? <span className="text-muted-foreground">—</span>}
                  </Td>
                  <Td>
                    <ProviderBadge provider={model.provider} />
                  </Td>
                  <Td className="text-muted-foreground font-mono mono-id text-xs">{model.model_id}</Td>
                  <Td numeric>${formatPer1k(model.input_price_per_1k)}/1K</Td>
                  <Td numeric>${formatPer1k(model.output_price_per_1k)}/1K</Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_creation_5m_price_per_1k > 0
                      ? `$${formatPer1k(model.cache_creation_5m_price_per_1k)}/1K`
                      : '—'}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_creation_1h_price_per_1k > 0
                      ? `$${formatPer1k(model.cache_creation_1h_price_per_1k)}/1K`
                      : '—'}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {model.cache_read_price_per_1k > 0
                      ? `$${formatPer1k(model.cache_read_price_per_1k)}/1K`
                      : '—'}
                  </Td>
                  <Td className="text-xs">
                    <AccountCell model={model} accounts={accounts} />
                  </Td>
                  <Td>
                    <StatusBadge isActive={model.is_active} activeLabel={t('active')} inactiveLabel={t('inactive')} />
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleEdit(model)}
                        className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      >
                        {t('edit')}
                      </button>
                      {model.is_active ? (
                        <button
                          onClick={() => handleDeactivate(model)}
                          className="inline-flex items-center justify-center rounded-md border border-destructive/30 bg-background px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        >
                          {t('deactivate')}
                        </button>
                      ) : (
                        <button
                          onClick={() => handleActivate(model)}
                          disabled={isPending}
                          className="inline-flex items-center justify-center rounded-md border border-primary/40 bg-background px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                        >
                          {t('activate')}
                        </button>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
        </Table>
      </div>

      <CreateModelDialog
        isOpen={editDialogOpen}
        onClose={() => {
          setEditDialogOpen(false);
          setSelectedModel(null);
        }}
        editModel={selectedModel ?? undefined}
        accounts={accounts}
      />

      <DeactivateModelDialog
        isOpen={deactivateDialogOpen}
        onClose={() => {
          setDeactivateDialogOpen(false);
          setSelectedModel(null);
        }}
        model={selectedModel}
      />
    </>
  );
}