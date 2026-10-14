'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { TeamBudgetAllocation, AllocationEntry } from '@/types/entities';
import { allocateTeamBudgetAction } from '@/lib/actions/budgets';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Table, THead, TBody, TFoot, Tr, Th, Td, TEmpty } from '@/components/common/Table';

interface TeamAllocationViewProps {
  teamId: string;
  initialAllocation: TeamBudgetAllocation | null;
}

export function TeamAllocationView({ teamId, initialAllocation }: TeamAllocationViewProps) {
  const t = useTranslations('budgets');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();

  // 배정 가능한 행은 **멤버(USER)** 뿐이다. GET /allocation 의 첫 항목은 팀 합계 행
  // (target_type="TEAM", allocated_usd = 팀 총예산)이라, 그대로 두면 팀이 가짜 멤버로 그려지고
  // totalAllocated 에 총예산이 한 번 더 더해져 멤버에게 1달러만 배정해도 isOverBudget 이 참 —
  // 저장 버튼이 영구히 막혔다. 쓰기(allocateTeamBudgetAction)도 USER 만 보낸다.
  const [allocations, setAllocations] = useState<AllocationEntry[]>(
    (initialAllocation?.entries ?? []).filter((e) => e.target_type === 'USER')
  );

  // *_usd 는 Decimal-as-string 이다(entities.ts) — 산술 전에 숫자로 바꾼다. 문자열 그대로면
  // `0 + "60.0000"` 이 문자열 연결이 되고 `.toFixed()` 는 TypeError 로 렌더를 죽인다.
  //
  // ⚠️ 합계·비교는 1/10000 달러 **정수**로 한다(DB 컬럼 NUMERIC(12,4), 서버 검사는 Decimal).
  //    부동소수로 더하면 50.10 + 50.20 = 100.30000000000001 이라 $100.30 을 정확히 다 배정한
  //    순간 '초과' 로 판정돼 저장이 막혔다(미배정 칸은 $-0.00).
  const toUnits = (usd: unknown) => Math.round((Number(usd) || 0) * 10_000);
  const totalBudgetUnits = toUnits(initialAllocation?.total_budget_usd);
  const totalAllocatedUnits = allocations.reduce((sum, entry) => sum + toUnits(entry.allocated_usd), 0);
  const totalBudget = totalBudgetUnits / 10_000;
  const totalAllocated = totalAllocatedUnits / 10_000;
  const unallocated = (totalBudgetUnits - totalAllocatedUnits) / 10_000;
  const isOverBudget = totalAllocatedUnits > totalBudgetUnits;

  const handleAllocationChange = (targetId: string, newValue: number) => {
    setAllocations((prev) =>
      prev.map((entry) =>
        entry.target_id === targetId
          ? { ...entry, allocated_usd: isNaN(newValue) ? '0' : String(newValue) }
          : entry
      )
    );
  };

  const handleSave = () => {
    startTransition(async () => {
      const result = await allocateTeamBudgetAction(teamId, allocations);

      if (result.success) {
        toast({
          type: 'success',
          message: t('allocationSaved'),
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

  if (!initialAllocation) {
    return (
      <p className="text-sm text-muted-foreground">{t('allocationLoadFailed')}</p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 flex items-center justify-between">
        <span className="text-sm font-medium">
          {t('teamTotalBudget', { team: initialAllocation.team_name })}
        </span>
        <span className="text-lg font-bold">${totalBudget.toFixed(2)}</span>
      </div>

      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('colMemberName')}</Th>
              <Th>{t('colAllocated')}</Th>
              <Th numeric>{t('colUsage')}</Th>
            </Tr>
          </THead>
          <TBody>
            {allocations.length === 0 ? (
              <TEmpty colSpan={3}>{t('noMembers')}</TEmpty>
            ) : (
              allocations.map((entry) => (
                <Tr key={entry.target_id}>
                  <Td emphasis>{entry.target_name}</Td>
                  <Td>
                    <div className="flex items-center gap-1">
                      <span className="text-muted-foreground">$</span>
                      <input
                        type="number"
                        min={0}
                        step={0.01}
                        value={entry.allocated_usd}
                        onChange={(e) =>
                          handleAllocationChange(entry.target_id, parseFloat(e.target.value))
                        }
                        className="w-32 rounded-md border border-input bg-background px-2 py-1 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      />
                    </div>
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    ${(Number(entry.used_usd) || 0).toFixed(2)}
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
          <TFoot>
            <Tr>
              <Td emphasis colSpan={2}>{t('unallocated')}</Td>
              <Td numeric className={isOverBudget ? 'text-destructive' : 'text-primary'}>
                ${unallocated.toFixed(2)}
              </Td>
            </Tr>
          </TFoot>
        </Table>
      </div>

      {isOverBudget && (
        <p className="text-sm text-destructive" role="alert">
          {t('allocationExceeds', { allocated: totalAllocated.toFixed(2), budget: totalBudget.toFixed(2) })}
        </p>
      )}

      <div className="flex justify-end">
        <SpinnerButton
          onClick={handleSave}
          isLoading={isPending}
          disabled={isOverBudget}
          type="button"
        >
          {tCommon('save')}
        </SpinnerButton>
      </div>
    </div>
  );
}