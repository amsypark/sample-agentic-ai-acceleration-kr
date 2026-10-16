'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Globe, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import type { RateLimitScope } from '@/types/enums';

interface RateLimitTreeProps {
  nodes: RateLimitTreeNode[];
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth?: number;
}

/**
 * 트리 행 우측의 한 줄 요약.
 *
 * 예전에는 언제나 `${rpm ?? '∞'}rpm` 이었다. GLOBAL(Bedrock 쿼터) 한도는 TPM 만 걸리는 경우가
 * 흔해서, 200K TPM 캡이 걸린 모델이 트리에서 "∞rpm" 으로 — 즉 무제한처럼 — 보였다. 설정된
 * 축을 그대로 보여준다. cpm/cph 는 와이어에서 문자열(Decimal)이다.
 */
function summarizeLimit(config: RateLimitTreeNode['config']): string {
  if (!config) return '-';
  if (config.rpm != null) return `${config.rpm}rpm`;
  if (config.tpm != null) return `${config.tpm}tpm`;
  if (config.cpm != null) return `$${config.cpm}/m`;
  if (config.cph != null) return `$${config.cph}/h`;
  return '∞';
}

function NodeIcon({ scope }: { scope: RateLimitScope }) {
  if (scope === 'GLOBAL') return <Globe size={14} className="flex-shrink-0" aria-hidden="true" />;
  if (scope === 'TEAM') return <Users size={14} className="flex-shrink-0" aria-hidden="true" />;
  return <User size={14} className="flex-shrink-0" aria-hidden="true" />;
}

interface TreeNodeProps {
  node: RateLimitTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth: number;
}

function TreeNode({
  node,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth,
}: TreeNodeProps) {
  const t = useTranslations('rateLimits');
  const isSelected = selectedNodeId === node.id;
  const hasChildren = Boolean(node.children && node.children.length > 0);
  // USER 와 GLOBAL 은 leaf. 자식을 가지는 것은 TEAM(멤버) 뿐이다.
  const isExpandable = node.scope !== 'USER' && hasChildren;
  const isExpanded = expandedNodes.has(node.id);

  const handleClick = () => {
    onSelect(node);
    if (isExpandable) {
      onToggle(node.id);
    }
  };

  return (
    <div>
      <button
        onClick={handleClick}
        className={[
          'w-full flex items-center gap-2 py-2 text-sm hover:bg-accent/50 transition-colors text-left',
          isSelected ? 'bg-accent text-accent-foreground' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        style={{ paddingLeft: `${12 + depth * 16}px`, paddingRight: '12px' }}
      >
        {isExpandable ? (
          isExpanded ? (
            <ChevronDown size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <ChevronRight size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          )
        ) : (
          <span className="w-3 flex-shrink-0" aria-hidden="true" />
        )}

        <NodeIcon scope={node.scope} />
        <span className="flex-1 truncate">{node.label}</span>
        <span className="text-xs text-muted-foreground flex-shrink-0">
          {node.inherited_from ? t('inherited') : summarizeLimit(node.config)}
        </span>
      </button>

      {isExpanded && hasChildren && node.children.map((child) => (
        <TreeNode
          key={child.id}
          node={child}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth + 1}
        />
      ))}
    </div>
  );
}

export function RateLimitTree({
  nodes,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth = 0,
}: RateLimitTreeProps) {
  const t = useTranslations('rateLimits');
  if (nodes.length === 0) {
    return (
      <p className="p-4 text-sm text-muted-foreground">{t('noConfigData')}</p>
    );
  }

  return (
    <div className="py-1">
      {nodes.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth}
        />
      ))}
    </div>
  );
}
