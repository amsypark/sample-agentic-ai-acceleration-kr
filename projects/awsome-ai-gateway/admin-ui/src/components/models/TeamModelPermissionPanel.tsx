'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import {
  getTeamAllowedModelsAction,
  setTeamAllowedModelsAction,
  clearTeamAllowedModelsAction,
} from '@/lib/actions/models';
import type { ModelListItem } from '@/types/entities';

interface TeamOption {
  id: string;
  name: string;
}

/**
 * 허용 모델 조회의 3-state.
 *
 * boolean `loaded` 하나이던 동안 조회 **실패** 가 "제한 없음 (전체 허용)" 배지와 같은 화면이었다 —
 * 서버는 빈 목록으로 "제한 없음" 을 표현하는데(행 존재 규약), 실패 분기도 똑같이
 * `setAllowedAliases([])` 를 했기 때문이다. whitelist 가 걸린 팀이 무제한으로 읽히면 운영자는 통제가
 * 없다고 판단하고, 그 상태에서 체크해 저장하면 실제 whitelist 를 모르는 채 덮어쓴다. 'failed' 는
 * "모른다" 이고 'ok' 만이 서버가 실제로 답한 상태다 — 배지와 편집 UI 는 'ok' 에서만 그린다.
 */
type LoadState = 'unknown' | 'ok' | 'failed';

interface TeamModelPermissionPanelProps {
  teams: TeamOption[];
  allTeams?: TeamOption[];
  models: ModelListItem[];
}

export function TeamModelPermissionPanel({ teams, allTeams, models }: TeamModelPermissionPanelProps) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [selectedTeamId, setSelectedTeamId] = useState('');
  const [allowedAliases, setAllowedAliases] = useState<string[]>([]);
  const [hasRestrictions, setHasRestrictions] = useState(false);
  const [load, setLoad] = useState<LoadState>('unknown');
  const [loadError, setLoadError] = useState<string | null>(null);
  // 다시 시도용 트리거 — 같은 팀을 다시 조회해야 하므로 selectedTeamId 만으로는 effect 가 돌지 않는다.
  const [reloadNonce, setReloadNonce] = useState(0);
  const [showInactive, setShowInactive] = useState(false);
  const loaded = load === 'ok';

  const visibleTeams = showInactive && allTeams ? allTeams : teams;

  const activeModels = models.filter(m => m.is_active);

  useEffect(() => {
    if (!selectedTeamId) {
      setAllowedAliases([]);
      setHasRestrictions(false);
      setLoad('unknown');
      setLoadError(null);
      return;
    }
    // 팀을 바꾸면 이전 팀의 응답 상태를 즉시 버린다 — 'ok' 로 남겨 두면 조회가 끝나기 전에 이전 팀의
    // 체크박스가 **새 팀의 정책인 것처럼** 그려진다.
    setLoad('unknown');
    setLoadError(null);
    let cancelled = false;
    startTransition(async () => {
      let result;
      try {
        result = await getTeamAllowedModelsAction(selectedTeamId);
      } catch (err) {
        // server action 안의 try/catch 는 **서버 쪽**이다. 클라이언트→서버 RPC 자체가 끊기면
        // (배포 스큐, 오프라인) 이 프로미스가 거부되고, 예전엔 화면이 영원히 "로딩 중" 이었다.
        if (cancelled) return;
        setLoad('failed');
        setLoadError(err instanceof Error ? err.message : String(err));
        return;
      }
      if (cancelled) return;
      if (result.success) {
        setAllowedAliases(result.data.model_aliases);
        setHasRestrictions(result.data.model_aliases.length > 0);
        setLoad('ok');
      } else {
        // ★ allowedAliases / hasRestrictions 는 **건드리지 않는다**. 빈 목록은 서버가 "제한 없음" 을
        //   뜻하는 실제 값이므로, 읽지도 못한 상태를 그 값으로 덮으면 실패가 "제한 없음" 주장이 된다.
        setLoad('failed');
        setLoadError(result.error);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [selectedTeamId, reloadNonce]);

  const toggleModel = (alias: string) => {
    setAllowedAliases(prev =>
      prev.includes(alias) ? prev.filter(a => a !== alias) : [...prev, alias]
    );
  };

  const handleSave = () => {
    if (!selectedTeamId) return;
    if (allowedAliases.length === 0) {
      toast({ type: 'error', message: t('minOneModel'), auto_dismiss_ms: 3000 });
      return;
    }
    startTransition(async () => {
      const result = await setTeamAllowedModelsAction(selectedTeamId, allowedAliases);
      if (result.success) {
        setHasRestrictions(true);
        toast({ type: 'success', message: t('teamPermissionSaved'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  const handleClear = () => {
    if (!selectedTeamId) return;
    startTransition(async () => {
      const result = await clearTeamAllowedModelsAction(selectedTeamId);
      if (result.success) {
        setAllowedAliases([]);
        setHasRestrictions(false);
        toast({ type: 'success', message: t('restrictionCleared'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  return (
    <div className="space-y-4 glass rounded-apple p-4">
      <div className="flex items-center gap-4">
        <label className="text-sm font-medium">{t('selectTeam')}</label>
        <select
          value={selectedTeamId}
          onChange={e => setSelectedTeamId(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
        >
          <option value="">{t('selectTeamPlaceholder')}</option>
          {visibleTeams.map(t => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
        {allTeams && allTeams.length > teams.length && (
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            {t('includeInactiveTeams')}
          </label>
        )}
        {selectedTeamId && loaded && (
          hasRestrictions ? (
            <span className="badge badge-amber">{t('restrictionApplied')}</span>
          ) : (
            <span className="badge badge-teal">{t('noRestriction')}</span>
          )
        )}
      </div>

      {selectedTeamId && loaded && (
        <>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">{t('selectAllowedModels')}</span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setAllowedAliases(activeModels.map(m => m.alias))}
                  className="text-xs text-primary hover:underline"
                >
                  {t('selectAll')}
                </button>
                <button
                  type="button"
                  onClick={() => setAllowedAliases([])}
                  className="text-xs text-muted-foreground hover:underline"
                >
                  {t('clearAll')}
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
              {activeModels.map(m => (
                <label key={m.alias} className="flex items-center gap-2 rounded-md border p-2 cursor-pointer hover:bg-muted/50">
                  <input
                    type="checkbox"
                    checked={allowedAliases.includes(m.alias)}
                    onChange={() => toggleModel(m.alias)}
                    className="h-4 w-4 rounded border-gray-300"
                  />
                  <span className="text-sm">{m.alias}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-3 pt-2 border-t">
            <SpinnerButton
              onClick={handleSave}
              isLoading={isPending}
              className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-md text-sm font-medium"
            >
              {t('savePermission')}
            </SpinnerButton>
            {hasRestrictions && (
              <button
                type="button"
                onClick={handleClear}
                disabled={isPending}
                className="text-sm text-destructive hover:underline disabled:opacity-50"
              >
                {t('clearRestriction')}
              </button>
            )}
          </div>
        </>
      )}

      {/* ★ 실패는 "제한 없음" 도 아니고 "로딩 중" 도 아니다. 스피너로 그리면 화면이 영원히 로딩처럼
          보이고, 배지로 그리면 읽지도 못한 상태를 "전체 허용" 이라고 단정한다. 그래서 이 자리는
          "모른다 + 다시 시도" 만 말한다 — 편집 UI 는 닫혀 있다. */}
      {selectedTeamId && load === 'failed' && (
        <div
          role="alert"
          className="space-y-2 rounded-apple-md border border-destructive/40 bg-destructive/5 p-3"
        >
          <p className="text-sm text-destructive">{t('teamPermissionLoadFailed', { error: loadError ?? '' })}</p>
          <button
            type="button"
            onClick={() => setReloadNonce(n => n + 1)}
            disabled={isPending}
            className="text-xs text-primary hover:underline disabled:opacity-50"
          >
            {t('retryLoad')}
          </button>
        </div>
      )}

      {selectedTeamId && load === 'unknown' && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          {t('loadingText')}
        </div>
      )}
    </div>
  );
}