'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useEffect, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitScope } from '@/types/enums';
import { setRateLimitAction, deleteUserRateLimitAction } from '@/lib/actions/rate-limits';
import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface RateLimitConfigPanelProps {
  node: RateLimitTreeNode | null;
  /**
   * 지금 활성 GLOBAL 한도를 가진 모델 alias(없으면 null). GLOBAL 은 현재 **한 번에 한 모델**만
   * 활성 한도를 가질 수 있다(admin-api upsert 가 alias 와 무관하게 GLOBAL 활성 행을 전부
   * 비활성화한다) — 다른 모델에 저장하기 전에 그 사실을 알린다.
   */
  globalSlotHolder?: string | null;
}

// SCOPE_LABEL is now resolved inside the component via translations

function parsePosInt(val: string): number | null {
  const n = parseInt(val, 10);
  return isNaN(n) || n <= 0 ? null : n;
}

function parsePosFloat(val: string): number | null {
  const n = parseFloat(val);
  return isNaN(n) || n <= 0 ? null : n;
}

export function RateLimitConfigPanel({ node, globalSlotHolder = null }: RateLimitConfigPanelProps) {
  const t = useTranslations('rateLimits');
  const tCommon = useTranslations('common');
  const { toast } = useToast();

  const SCOPE_LABEL: Record<string, string> = {
    GLOBAL: t('scopeLabel.GLOBAL'),
    TEAM: t('scopeLabel.TEAM'),
    USER: t('scopeLabel.USER'),
  };
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const [rpm, setRpm] = useState('');
  const [tpm, setTpm] = useState('');
  const [cpm, setCpm] = useState('');
  const [cph, setCph] = useState('');
  const [usage, setUsage] = useState<RateLimitUsage | null>(null);

  // node가 변경될 때 폼 값 초기화.
  //
  // ⚠️ 프리필 조건은 "config 가 있는가" 가 아니라 **"이 노드 자신의 설정인가"** 다. 상속 중인
  //    USER 노드의 `config` 는 팀의 행(config.scope="TEAM", inherited_from 설정)이다. 그 값을
  //    입력칸에 채우면 CPH 하나만 고치려고 저장해도 PUT 이 전체 교체이므로 팀의 RPM/TPM 이 그대로
  //    **새 USER 행**에 굳는다. 이후 팀 한도를 올려도 그 사용자만 옛 값에 고정된다(gateway-proxy 는
  //    스코프별로 독립 집행한다). 상속값은 placeholder 로만 보여준다.
  useEffect(() => {
    const own = node?.config && !node.inherited_from ? node.config : null;
    setRpm(own?.rpm != null ? String(own.rpm) : '');
    setTpm(own?.tpm != null ? String(own.tpm) : '');
    // cpm/cph 는 와이어에서 문자열(Decimal)이다 — String() 은 방어적 통일용.
    setCpm(own?.cpm != null ? String(own.cpm) : '');
    setCph(own?.cph != null ? String(own.cph) : '');
    setError(null);
  }, [node]);

  // 실시간 RPM 사용량(§60.9) — node 선택 시 + 10초마다 폴링(gateway-proxy Redis 카운터).
  // GLOBAL 도 폴링한다: proxy 는 GLOBAL 스코프에도 카운터를 쌓고(키의 scope_id 자리는 리터럴
  // '*', 모델은 model_alias 자리) GLOBAL 노드 id 가 곧 model alias 다.
  useEffect(() => {
    if (!node) {
      setUsage(null);
      return;
    }
    let alive = true;
    const load = () =>
      fetchRateLimitUsage(node.scope, node.id)
        .then((u) => { if (alive) setUsage(u); })
        .catch(() => { if (alive) setUsage(null); });
    load();
    const t = setInterval(load, 10_000);
    return () => { alive = false; clearInterval(t); };
  }, [node]);

  if (!node) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        {t('selectNodePrompt')}
      </div>
    );
  }

  const isUserOrTeam =
    node.scope === RateLimitScope.USER || node.scope === RateLimitScope.TEAM;

  // 상속값은 입력값이 아니라 placeholder 로만 노출한다(위 프리필 주석 참조).
  const inherited = node.inherited_from ? node.config : null;
  const inheritedRpm = inherited?.rpm != null ? String(inherited.rpm) : undefined;
  const inheritedTpm = inherited?.tpm != null ? String(inherited.tpm) : undefined;
  const inheritedCpm = inherited?.cpm != null ? String(inherited.cpm) : undefined;
  const inheritedCph = inherited?.cph != null ? String(inherited.cph) : undefined;

  // 상속 중이면 "비워두면 제한 없음" 은 거짓이다 — 비워두면 팀 한도가 계속 적용된다.
  const emptyHint = node.inherited_from ? t('emptyMeansInherit') : t('emptyMeansUnlimited');

  // 이 노드 자신의 USER 행이 있을 때만 '개인 한도 제거' 가 의미를 가진다.
  const canRemoveOverride =
    node.scope === RateLimitScope.USER && !!node.config && !node.inherited_from;

  // GLOBAL 은 한 번에 한 모델만 — 다른 모델이 슬롯을 갖고 있으면 저장 전에 알린다.
  const replacesGlobalOn =
    node.scope === RateLimitScope.GLOBAL && globalSlotHolder && globalSlotHolder !== node.id
      ? globalSlotHolder
      : null;

  // 게이지 분모는 **이 스코프 자신의** 한도여야 한다. 상속 노드의 config.rpm 은 팀 값이고 그
  // 카운터는 TEAM 키에 쌓이므로, USER 사용량을 팀 한도로 나누면 거짓 비율이 된다.
  const gaugeLimit = usage?.limit_rpm ?? (node.inherited_from ? null : node.config?.rpm ?? null);

  const handleRemoveOverride = () => {
    setError(null);
    startTransition(async () => {
      const result = await deleteUserRateLimitAction(node.id);
      if (result.success) {
        toast({
          type: 'success',
          message: t('overrideRemoved', { label: node.label }),
          auto_dismiss_ms: 3000,
        });
      } else {
        setError(result.error);
      }
    });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    startTransition(async () => {
      const result = await setRateLimitAction({
        target_id: node.id,
        scope: node.scope,
        rpm: parsePosInt(rpm),
        tpm: parsePosInt(tpm),
        cpm: isUserOrTeam ? parsePosFloat(cpm) : undefined,
        cph: isUserOrTeam ? parsePosFloat(cph) : undefined,
      });

      if (result.success) {
        toast({
          type: 'success',
          message: t('saved', { label: node.label }),
          auto_dismiss_ms: 3000,
        });
      } else {
        setError(result.error);
      }
    });
  };

  return (
    <div>
      {/* 헤더 */}
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold">{node.label}</h2>
        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold">
          {SCOPE_LABEL[node.scope] ?? node.scope}
        </span>
      </div>

      {/* 상속 정보 */}
      {node.inherited_from && (
        <div className="mb-4 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
          {t('inheritingFrom')} <span className="font-medium text-foreground">{node.inherited_from}</span>
        </div>
      )}

      {/* GLOBAL 단일 슬롯 경고 — 저장하면 다른 모델의 GLOBAL 한도가 해제된다. */}
      {replacesGlobalOn && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400"
        >
          {t('globalSlotWarning', { alias: replacesGlobalOn })}
        </div>
      )}

      {/* 실시간 사용량(§60.9) — gateway-proxy Redis sliding-window 카운터. 10초 폴링.
          measured=false 면 이 스코프에는 카운터가 **존재할 수 없다**(자기 rpm 한도가 없으면 proxy 가
          ZSET 을 만들지 않는다). 그때 0 을 "요청 없음" 으로 쓰면 실제로 게이트웨이를 포화시키는 중인
          사용자를 '유휴' 로 보이게 만든다 — 한도를 걸지 판단하려는 운영자에게 가장 해로운 거짓이다. */}
      {usage?.available && (
        <div className="mb-4 rounded-md border border-border bg-card/50 px-3 py-2.5">
          {usage.measured ? (
            <>
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  {t('liveRpm', { seconds: usage.window_sec })}
                </span>
                <span className="num text-sm font-semibold text-foreground">
                  {usage.rpm_used_total}
                  {gaugeLimit != null && (
                    <span className="text-muted-foreground font-normal"> / {gaugeLimit}</span>
                  )}
                </span>
              </div>
              {/* 한도 대비 게이지(자기 한도가 있을 때만) */}
              {gaugeLimit != null && gaugeLimit > 0 && (
                <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-primary transition-all"
                    style={{ width: `${Math.min(100, (usage.rpm_used_total / gaugeLimit) * 100)}%` }}
                  />
                </div>
              )}
              {usage.by_model.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-[11px] text-muted-foreground">
                  {usage.by_model.slice(0, 5).map((m) => (
                    <li key={m.model_alias} className="flex justify-between">
                      <span className="font-mono">{m.model_alias}</span>
                      <span className="num">{m.rpm_used}</span>
                    </li>
                  ))}
                </ul>
              )}
              {usage.rpm_used_total === 0 && (
                <p className="mt-1 text-[11px] text-muted-foreground">{t('noRecentRequests', { seconds: usage.window_sec })}</p>
              )}
            </>
          ) : (
            <div>
              <span className="text-xs font-medium text-muted-foreground">
                {t('liveRpm', { seconds: usage.window_sec })}
              </span>
              <p className="mt-1 text-[11px] text-muted-foreground">{t('usageNotMeasured')}</p>
            </div>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* RPM */}
        <div className="space-y-1">
          <label htmlFor="rl-rpm" className="text-sm font-medium">RPM (Requests Per Minute)</label>
          <input
            id="rl-rpm"
            type="number"
            min={1}
            value={rpm}
            onChange={(e) => setRpm(e.target.value)}
            placeholder={inheritedRpm ?? t('unlimited')}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">{emptyHint}</p>
        </div>

        {/* TPM */}
        <div className="space-y-1">
          <label htmlFor="rl-tpm" className="text-sm font-medium">TPM (Tokens Per Minute)</label>
          <input
            id="rl-tpm"
            type="number"
            min={1}
            value={tpm}
            onChange={(e) => setTpm(e.target.value)}
            placeholder={inheritedTpm ?? t('unlimited')}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">{emptyHint}</p>
        </div>

        {/* CPM — USER/TEAM only */}
        <div className="space-y-1">
          <label
            htmlFor="rl-cpm"
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPM (Cost Per Minute, USD)
          </label>
          <input
            id="rl-cpm"
            type="number"
            min={0}
            step="0.0001"
            value={cpm}
            onChange={(e) => setCpm(e.target.value)}
            placeholder={inheritedCpm ?? t('unlimited')}
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">{t('scopeOnlyUserTeam')}</p>
          )}
        </div>

        {/* CPH — USER/TEAM only */}
        <div className="space-y-1">
          <label
            htmlFor="rl-cph"
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPH (Cost Per Hour, USD)
          </label>
          <input
            id="rl-cph"
            type="number"
            min={0}
            step="0.0001"
            value={cph}
            onChange={(e) => setCph(e.target.value)}
            placeholder={inheritedCph ?? t('unlimited')}
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">{t('scopeOnlyUserTeam')}</p>
          )}
        </div>

        <FormError error={error} />

        {/* 입력을 모두 비우고 저장하는 것은 '제거' 가 아니다 — 전부 NULL 인 USER 행이 남아 상속
            표식이 사라지고, 팀 한도가 걸려 있는데도 무제한처럼 보인다. 실제 제거는
            DELETE /admin/rate-limits/user/{id} 로만 가능하다. */}
        <div className="flex items-center justify-between pt-2">
          {canRemoveOverride ? (
            <button
              type="button"
              onClick={handleRemoveOverride}
              disabled={isPending}
              className="rounded-md border border-input px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {t('removeOverride')}
            </button>
          ) : (
            <span />
          )}
          <SpinnerButton type="submit" isLoading={isPending}>
            {tCommon('save')}
          </SpinnerButton>
        </div>
      </form>
    </div>
  );
}