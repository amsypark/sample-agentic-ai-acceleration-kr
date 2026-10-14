// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모니터링 모델 표의 상태 배지 — 서버가 이제 등록부의 실제 status 를 보낸다.
 *
 * 예전 서버는 `"status": "ACTIVE"` 리터럴이었고 표는 `status === 'ACTIVE' ? teal : neutral` 이라,
 * neutral 분기는 도달 불가였고 INACTIVE 로 내린 모델도 초록이었다. 이제 ACTIVE / INACTIVE /
 * UNREGISTERED(등록부에 없는 alias 로 트래픽이 흐름)를 각자 다른 톤으로 그린다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import type { MonitoringModelsResponse } from '@/lib/actions/monitoring';

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));

import { ModelHealthTable } from '@/components/monitoring/ModelHealthTable';

function row(alias: string, status: string) {
  return {
    alias,
    status,
    last_1h_requests: 3,
    avg_latency_ms: 100,
    error_rate_pct: 0,
    last_request_at: null,
  };
}

describe('ModelHealthTable status badge', () => {
  it('gives each registry state its own tone', () => {
    const data = {
      models: [row('live', 'ACTIVE'), row('retired', 'INACTIVE'), row('ghost', 'UNREGISTERED')],
    } as MonitoringModelsResponse;
    render(<ModelHealthTable data={data} />);

    expect(screen.getByText('ACTIVE')).toHaveClass('badge-teal');
    expect(screen.getByText('INACTIVE')).toHaveClass('badge-neutral');
    expect(screen.getByText('UNREGISTERED')).toHaveClass('badge-amber');
  });

  it('an unknown future status falls back to neutral with its raw label', () => {
    const data = { models: [row('m', 'DEPRECATED')] } as unknown as MonitoringModelsResponse;
    render(<ModelHealthTable data={data} />);
    expect(screen.getByText('DEPRECATED')).toHaveClass('badge-neutral');
  });
});
