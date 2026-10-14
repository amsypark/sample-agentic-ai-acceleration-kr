'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import type { MonitoringModelStatus, MonitoringModelsResponse } from '@/lib/actions/monitoring';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 에러율 강조 — 테마 토큰(다크/라이트 자동). 임계: ≥10% 위험, ≥5% 경고.
function errorColor(pct: number) {
  if (pct >= 10) return 'text-destructive font-semibold';
  if (pct >= 5) return 'text-amber-600 dark:text-amber-400';
  return '';
}

/**
 * 상태 → 배지 톤. **명시적인 표**여야 한다: 예전 `m.status === 'ACTIVE' ? 'teal' : 'neutral'` 은 서버가
 * 리터럴 'ACTIVE' 를 보내던 동안 neutral 분기가 도달 불가였다. 삼항이면 새 상태가 조용히 "그 외" 로
 * 접히지만 `Record<MonitoringModelStatus, …>` 는 타입 검사에서 걸린다.
 * UNREGISTERED = amber(경고): 등록부에 없는 alias 로 트래픽이 흐른 상태다 — 정상도 아니고(초록 아님)
 * 단순 비활성도 아니다(회색 아님).
 */
const STATUS_TONE: Record<MonitoringModelStatus, BadgeTone> = {
  ACTIVE: 'teal',
  INACTIVE: 'neutral',
  UNREGISTERED: 'amber',
};

export function ModelHealthTable({ data }: { data: MonitoringModelsResponse }) {
  const t = useTranslations('monitoring');

  if (data.models.length === 0) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">{t('models.empty')}</p>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold tracking-tight">{t('models.title')}</h3>
      </div>
      <Table>
        <THead>
          <Tr>
            <Th>{t('table.model')}</Th>
            <Th>{t('table.status')}</Th>
            <Th numeric>{t('table.requests')}</Th>
            <Th numeric>{t('table.avgLatency')}</Th>
            <Th numeric>{t('table.errorRate')}</Th>
            <Th numeric>{t('table.lastRequest')}</Th>
          </Tr>
        </THead>
        <TBody>
          {data.models.map((m) => (
            <Tr key={m.alias}>
              <Td emphasis className="font-mono mono-id text-xs">{m.alias}</Td>
              <Td>
                {/* 표에 없는 값(서버가 먼저 배포된 경우)은 neutral + 원문 그대로. */}
                <Badge tone={STATUS_TONE[m.status] ?? 'neutral'}>{m.status}</Badge>
              </Td>
              <Td numeric>{m.last_1h_requests.toLocaleString()}</Td>
              <Td numeric>{m.avg_latency_ms}ms</Td>
              <Td numeric className={errorColor(m.error_rate_pct)}>
                {m.error_rate_pct}%
              </Td>
              <Td numeric className="text-muted-foreground">
                {m.last_request_at
                  ? new Date(m.last_request_at).toLocaleTimeString('ko-KR')
                  : '-'}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}