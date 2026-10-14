'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTree } from './RateLimitTree';
import { RateLimitConfigPanel } from './RateLimitConfigPanel';

interface RateLimitTreeViewProps {
  nodes: RateLimitTreeNode[];
}

const EXPANDED_NODES_STORAGE_KEY = 'rate-limits:tree:expandedNodes';

export function RateLimitTreeView({ nodes }: RateLimitTreeViewProps) {
  const t = useTranslations('rateLimits');
  const [selectedNode, setSelectedNode] = useState<RateLimitTreeNode | null>(null);
  // 최초 진입은 모두 접힌 상태다. 루트는 GLOBAL(모델별, children=[]) · TEAM(멤버를 자식으로) ·
  // 팀 미배정 USER(children=[]) 세 부류가 **형제로** 온다 — GLOBAL 은 모델 축이라 TEAM/USER 의
  // 부모가 아니다(admin-api get_rate_limit_tree). 예전의 "GLOBAL 만 펼침" 기본값은 GLOBAL 노드가
  // 트리에 없던 시절의 것이라 아무것도 펼치지 않았다. 펼침 상태는 아래 effect 가 복원한다.
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(() => new Set<string>());
  const [showInactive, setShowInactive] = useState(false);
  const hasMountedRef = useRef(false);

  const hasInactive = nodes.some(n => n.is_active === false);
  // GLOBAL 은 한 번에 한 모델만 활성 한도를 가진다 — 패널이 저장 전에 경고할 수 있게 넘긴다.
  const globalSlotHolder = nodes.find(n => n.scope === 'GLOBAL' && n.config)?.id ?? null;
  const filteredNodes = showInactive
    ? nodes
    : nodes.filter(n => n.is_active !== false).map(n => ({
        ...n,
        children: n.children.filter(c => c.is_active !== false),
      }));

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(EXPANDED_NODES_STORAGE_KEY);
      if (raw) {
        const ids = JSON.parse(raw) as unknown;
        if (Array.isArray(ids) && ids.every((x) => typeof x === 'string')) {
          setExpandedNodes(new Set(ids as string[]));
        }
      }
    } catch {
      // 손상/비활성 storage 무시
    }
  }, []);

  useEffect(() => {
    if (!hasMountedRef.current) {
      hasMountedRef.current = true;
      return;
    }
    try {
      sessionStorage.setItem(
        EXPANDED_NODES_STORAGE_KEY,
        JSON.stringify([...expandedNodes])
      );
    } catch {
      // quota/비활성 storage 무시
    }
  }, [expandedNodes]);

  const handleToggle = (id: string) => {
    setExpandedNodes((prev) => {
      if (prev.has(id)) {
        return new Set([...prev].filter((x) => x !== id));
      }
      return new Set([...prev, id]);
    });
  };

  return (
    <div className="space-y-3">
      {hasInactive && (
        <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={e => setShowInactive(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-gray-300"
          />
          {t('includeInactive')}
        </label>
      )}
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        <RateLimitTree
          nodes={filteredNodes}
          selectedNodeId={selectedNode?.id ?? null}
          expandedNodes={expandedNodes}
          onSelect={setSelectedNode}
          onToggle={handleToggle}
        />
      </div>
      <div className="flex-1 p-6">
        <RateLimitConfigPanel node={selectedNode} globalSlotHolder={globalSlotHolder} />
      </div>
      </div>
    </div>
  );
}