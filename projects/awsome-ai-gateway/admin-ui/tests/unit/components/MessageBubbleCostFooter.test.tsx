// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * MessageBubble 의 비용/지연 footer.
 *
 * admin-api 가 이제 done 프레임에 `durationMs` 를 싣고 같은 값을 messages.duration_ms 로 남긴다
 * (예전: done 은 `{}`, 컬럼은 영구 NULL → footer 가 아무 오류 없이 사라져 있었다). 값이 실제로
 * 오기 시작하면 가드가 문제가 된다: truthy 가드(`message.costUsd || message.durationMs`)는 진짜
 * 0 — 캐시 전부 히트한 턴의 `$0.0000`, 1ms 미만의 `0ms` — 을 "값 없음" 과 같게 접고,
 * `{0 && <span/>}` 는 React 가 숫자 0 을 그대로 찍어 footer 자리에 "0" 이 홀로 남는다.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

vi.mock('next-intl', () => ({
  useTranslations: (ns?: string) => (key: string, values?: Record<string, unknown>) => {
    const full = ns ? `${ns}.${key}` : key;
    return values ? `${full}:${Object.values(values).join(',')}` : full;
  },
  useLocale: () => 'ko',
}));

vi.mock('@/components/chat/ChartRenderer', () => ({
  ChartRenderer: () => <div data-testid="chart-renderer" />,
}));

import { MessageBubble } from '@/components/chat/MessageBubble';
import type { ChatMessage } from '@/components/chat/types';

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { id: 'm1', role: 'assistant', content: '분석 결과입니다.', ...overrides };
}

describe('cost / latency footer', () => {
  it('renders both when both are present (control)', () => {
    render(<MessageBubble message={message({ costUsd: 0.0123, durationMs: 4210 })} />);
    expect(screen.getByText('chat.costLabel:0.0123')).toBeInTheDocument();
    expect(screen.getByText('4210ms')).toBeInTheDocument();
  });

  it('renders the duration alone — the only field admin-api sends today', () => {
    render(<MessageBubble message={message({ durationMs: 4210 })} />);
    expect(screen.getByText('4210ms')).toBeInTheDocument();
    expect(screen.queryByText(/chat\.costLabel/)).toBeNull();
  });

  it('renders a genuine zero cost instead of hiding it', () => {
    render(<MessageBubble message={message({ costUsd: 0, durationMs: 4210 })} />);
    expect(screen.getByText('chat.costLabel:0.0000')).toBeInTheDocument();
  });

  it('renders a genuine zero duration instead of a stray "0"', () => {
    const { container } = render(<MessageBubble message={message({ durationMs: 0 })} />);
    expect(screen.getByText('0ms')).toBeInTheDocument();
    // truthy 가드였다면 `{0 && …}` 가 숫자 0 텍스트 노드만 남긴다.
    const strayZero = Array.from(container.querySelectorAll('div')).some(
      (d) => d.childNodes.length === 1 && d.textContent === '0',
    );
    expect(strayZero).toBe(false);
  });

  it('renders no footer when neither value is known', () => {
    render(<MessageBubble message={message({ pending: true })} />);
    expect(screen.queryByText(/ms$/)).toBeNull();
    expect(screen.queryByText(/chat\.costLabel/)).toBeNull();
  });
});
