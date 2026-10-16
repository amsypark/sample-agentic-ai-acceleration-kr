// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Rate Limits 트리 — "집행 표면 전체" 가 실제로 화면에 오는지.
 *
 * 원래 결함: API 트리는 TEAM 루트 + USER 자식만 내보냈다.
 *   · GLOBAL 노드가 없어 `PUT /admin/rate-limits/global/{model_alias}` 는 콘솔에서 **도달
 *     불가**였고, API 로 건 GLOBAL 한도가 화면에 나타나지 않았다. GLOBAL 을 다루는 UI 분기
 *     (Globe 아이콘, CPM/CPH 비활성)는 전부 죽은 코드였다.
 *   · 팀 미배정 사용자(`auth.users.team_id IS NULL`)도 어떤 루트에도 없었다.
 *   · 트리 행 요약이 언제나 `${rpm ?? '∞'}rpm` 이라 TPM 만 걸린 한도가 "∞rpm", 즉 무제한처럼 보였다.
 *
 * 픽스처는 admin-api 가 직접 직렬화한 트리다(admin-api tests/unit/test_rate_limit_tree.py
 * ::test_wire_matches_admin_ui_fixture 가 이 파일과 완전 일치를 단정한다).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { RateLimitTreeNode } from '@/types/entities';
import treeFixture from '../../fixtures/rate-limit-tree.json';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}:${Object.values(values).join(',')}` : key,
}));

vi.mock('@/lib/actions/rate-limits', () => ({
  setRateLimitAction: vi.fn().mockResolvedValue({ success: true }),
  deleteUserRateLimitAction: vi.fn().mockResolvedValue({ success: true }),
}));

const fetchRateLimitUsage = vi.fn();
vi.mock('@/lib/utils/rateLimitUsage', () => ({
  fetchRateLimitUsage: (...a: unknown[]) => fetchRateLimitUsage(...a),
}));

vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast: vi.fn() }) }));

import { RateLimitTreeView } from '@/components/rate-limits/RateLimitTreeView';

const nodes = treeFixture as unknown as RateLimitTreeNode[];
const globalCapped = nodes.find((n) => n.scope === 'GLOBAL' && n.config)!;
const globalUncapped = nodes.find((n) => n.scope === 'GLOBAL' && !n.config)!;
const teamNode = nodes.find((n) => n.scope === 'TEAM')!;
const teamlessNode = nodes.find((n) => n.scope === 'USER')!;

beforeEach(() => {
  vi.clearAllMocks();
  fetchRateLimitUsage.mockResolvedValue({
    available: false,
    measured: false,
    limit_rpm: null,
    window_sec: 60,
    rpm_used_total: 0,
    by_model: [],
  });
  sessionStorage.clear();
});

describe('RateLimitTreeView', () => {
  it('GLOBAL · TEAM · 팀 미배정 USER 세 부류를 모두 루트로 렌더한다', () => {
    render(<RateLimitTreeView nodes={nodes} />);

    expect(screen.getByText(globalCapped.label)).toBeInTheDocument();
    expect(screen.getByText(globalUncapped.label)).toBeInTheDocument();
    expect(screen.getByText(teamNode.label)).toBeInTheDocument();
    expect(screen.getByText(teamlessNode.label)).toBeInTheDocument();
  });

  it('TPM 만 걸린 GLOBAL 한도를 무제한(∞)이 아니라 TPM 값으로 보여준다', () => {
    render(<RateLimitTreeView nodes={nodes} />);

    expect(globalCapped.config?.rpm).toBeNull(); // 픽스처 전제: TPM 전용 쿼터
    expect(screen.getByText(`${globalCapped.config?.tpm}tpm`)).toBeInTheDocument();
    expect(screen.queryByText('∞rpm')).toBeNull();
  });

  it('한도가 없는 모델도 노드로 나와 새 GLOBAL 한도를 설정할 수 있다 — 단일 슬롯 경고와 함께', async () => {
    const user = userEvent.setup();
    render(<RateLimitTreeView nodes={nodes} />);

    await user.click(screen.getByText(globalUncapped.label));

    // 패널이 그 노드로 열린다 → PUT 대상 id(=alias)가 생긴다.
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledWith('GLOBAL', globalUncapped.id));
    expect(screen.getByLabelText('CPM (Cost Per Minute, USD)')).toBeDisabled();
    // 다른 모델(globalCapped)이 GLOBAL 슬롯을 갖고 있다 — 트리가 그 사실을 패널에 넘긴다.
    expect(screen.getByRole('alert')).toHaveTextContent(`globalSlotWarning:${globalCapped.id}`);
  });

  it('전부 비어 있는 GLOBAL 행은 슬롯 주인이 아니다 — 경고하지 않는다', async () => {
    // 값을 모두 비우고 저장한 GLOBAL 행은 아무것도 집행하지 않는다(admin-api 도 같은 기준으로 409 를 내지 않는다).
    const user = userEvent.setup();
    const empty = { ...globalCapped.config!, rpm: null, tpm: null };
    render(
      <RateLimitTreeView nodes={nodes.map((n) => (n.id === globalCapped.id ? { ...n, config: empty } : n))} />,
    );

    await user.click(screen.getByText(globalUncapped.label));
    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledWith('GLOBAL', globalUncapped.id));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('TEAM 은 접힌 채 시작하고 펼치면 멤버가 보인다', async () => {
    const user = userEvent.setup();
    render(<RateLimitTreeView nodes={nodes} />);

    const member = teamNode.children[0];
    expect(screen.queryByText(member.label)).toBeNull();

    await user.click(screen.getByText(teamNode.label));

    expect(screen.getByText(member.label)).toBeInTheDocument();
  });

  it('상속 중인 멤버는 값 대신 상속 표식을 보여준다', async () => {
    const user = userEvent.setup();
    render(<RateLimitTreeView nodes={nodes} />);
    await user.click(screen.getByText(teamNode.label));

    const own = teamNode.children.find((c) => !c.inherited_from)!;
    expect(screen.getByText('inherited')).toBeInTheDocument();
    expect(screen.getByText(`${own.config?.rpm}rpm`)).toBeInTheDocument();
  });

  it('개인 한도를 제거해 서버가 새 트리를 내리면 패널이 새 노드를 보여준다', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<RateLimitTreeView nodes={nodes} />);
    await user.click(screen.getByText(teamNode.label));
    const own = teamNode.children.find((c) => !c.inherited_from)!;
    await user.click(screen.getByText(own.label));

    expect(screen.getByRole('button', { name: 'removeOverride' })).toBeInTheDocument();
    expect(screen.getByLabelText('RPM (Requests Per Minute)')).toHaveValue(own.config!.rpm);

    // DELETE 후 revalidate — 같은 사용자가 이제 팀 한도를 상속한다(새 배열·새 객체).
    const fresh = nodes.map((n) =>
      n.id !== teamNode.id
        ? n
        : {
            ...n,
            children: n.children.map((c) =>
              c.id === own.id ? { ...c, config: teamNode.config, inherited_from: teamNode.id } : c,
            ),
          },
    );
    rerender(<RateLimitTreeView nodes={fresh} />);

    expect(screen.queryByRole('button', { name: 'removeOverride' })).toBeNull();
    expect(screen.getByText(teamNode.id)).toBeInTheDocument(); // 상속 안내
    // 지운 값이 입력칸에 남아 있으면 '저장' 한 번에 같은 오버라이드가 되살아난다.
    expect(screen.getByLabelText('RPM (Requests Per Minute)')).toHaveValue(null);
  });

  it('새 트리에서 선택한 노드가 사라지면 패널이 비워진다', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<RateLimitTreeView nodes={nodes} />);
    await user.click(screen.getByText(teamlessNode.label));
    expect(screen.getByLabelText('RPM (Requests Per Minute)')).toBeInTheDocument();

    rerender(<RateLimitTreeView nodes={nodes.filter((n) => n.id !== teamlessNode.id)} />);

    expect(screen.queryByLabelText('RPM (Requests Per Minute)')).toBeNull();
    expect(screen.getByText('selectNodePrompt')).toBeInTheDocument();
  });

  it('트리가 그대로면 다시 그려도 입력 중인 값을 지우지 않는다 (대조군)', async () => {
    // 매 렌더 노드를 새로 만드는 식으로 고치면 패널 effect 가 매번 돌아 타이핑이 지워진다.
    const user = userEvent.setup();
    const { rerender } = render(<RateLimitTreeView nodes={nodes} />);
    await user.click(screen.getByText(teamlessNode.label));
    await user.type(screen.getByLabelText('RPM (Requests Per Minute)'), '77');

    rerender(<RateLimitTreeView nodes={nodes} />);

    expect(screen.getByLabelText('RPM (Requests Per Minute)')).toHaveValue(77);
  });

  it('팀 미배정 사용자를 선택하면 USER 스코프 패널이 열린다', async () => {
    const user = userEvent.setup();
    render(<RateLimitTreeView nodes={nodes} />);

    await user.click(screen.getByText(teamlessNode.label));

    await waitFor(() => expect(fetchRateLimitUsage).toHaveBeenCalledWith('USER', teamlessNode.id));
    // USER 스코프이므로 CPM/CPH 는 편집 가능하다.
    expect(screen.getByLabelText('CPM (Cost Per Minute, USD)')).not.toBeDisabled();
  });
});
