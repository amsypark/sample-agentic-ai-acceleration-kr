// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * done 프레임 → 메시지 footer 값의 클라이언트 쪽 계약.
 *
 * admin-api 는 이제 `event: done` 에 `{"durationMs": N}` 을 싣는다(camelCase — useChatStream 이
 * 파싱한 JSON 을 그대로 spread 하므로 키가 필드명과 글자 그대로 같아야 한다). 그리고 done 이 값을
 * 싣지 않을 때(구버전 admin-api, 비용 산출 불가) applyEvent 가 기존 값을 undefined 로 덮어쓰지
 * 않아야 한다 — 예전에는 재구독/복원으로 채워진 값까지 지웠다.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('next-intl', () => ({ useTranslations: () => (key: string) => key }));

import { parseSseBlock } from '@/components/chat/useChatStream';
import { applyEvent } from '@/components/chat/ChatLayout';
import type { ChatMessage, StreamEvent } from '@/components/chat/types';

const pending: ChatMessage = { id: 'm1', role: 'assistant', content: '답', pending: true };

function parse(block: string): StreamEvent[] {
  const events: StreamEvent[] = [];
  parseSseBlock(block, (e) => events.push(e));
  return events;
}

describe('done frame', () => {
  it('carries the server durationMs onto the message', () => {
    // admin-api routers/chat_agent.py 가 실제로 보내는 프레임 모양(_sse("done", {"durationMs": …})).
    const [event] = parse('event: done\ndata: {"durationMs": 1234}');
    const msg = applyEvent(pending, event);
    expect(msg.pending).toBe(false);
    expect(msg.durationMs).toBe(1234);
  });

  it('snake_case would not reach the field (why the server must send camelCase)', () => {
    const [event] = parse('event: done\ndata: {"duration_ms": 1234}');
    expect(applyEvent(pending, event).durationMs).toBeUndefined();
  });

  it('a done frame without values keeps what the message already had', () => {
    const restored: ChatMessage = { ...pending, durationMs: 900, costUsd: 0.01 };
    const msg = applyEvent(restored, { type: 'done' });
    expect(msg.durationMs).toBe(900);
    expect(msg.costUsd).toBe(0.01);
  });
});
