import React, { useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle,
  Copy,
  FileText,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { copyTextToClipboard } from '../../lib/utils';
import { Button } from '../ui/button';

interface DoctorPanelProps {
  isPage?: boolean;
  logsPath?: string;
  titleKey?: string;
}

type CheckStatus = 'pass' | 'warn' | 'fail';

const STATUS_TONE: Record<
  CheckStatus,
  { row: string; icon: string; text: string; iconNode: React.ReactNode }
> = {
  pass: {
    row: '',
    icon: 'text-mint',
    text: 'text-foreground',
    iconNode: <CheckCircle className="size-4" />,
  },
  warn: {
    row: 'bg-gold/[0.05]',
    icon: 'text-gold',
    text: 'text-gold font-semibold',
    iconNode: <AlertTriangle className="size-4" />,
  },
  fail: {
    row: 'bg-danger/[0.06]',
    icon: 'text-danger',
    text: 'text-danger font-semibold',
    iconNode: <XCircle className="size-4" />,
  },
};

// Mirrors design.pen Hns4E (VR/CM/Diagnostics):
// diagHero: cornerRadius 14, fill --surface-2, mint stroke 33%, blur 32 y16 spread -8 #5BFFA014.
// diagPulse: 42×42 mint-soft circle with blur 16 #5BFFA070 spread -4 glow.
// diagG: cornerRadius 12, fill --background, stroke --border. Item rows [10, 20].
export const DoctorPanel: React.FC<DoctorPanelProps> = ({
  isPage,
  logsPath = '/doctor/logs',
  titleKey = 'doctor.title',
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [loadingMode, setLoadingMode] = useState<'fast' | 'deep' | null>(null);
  const [results, setResults] = useState<any>(null);

  const runDoctor = async (deep = false) => {
    const mode = deep ? 'deep' : 'fast';
    setLoadingMode(mode);
    try {
      const res = await api.doctor({ deep });
      setResults(res);
    } finally {
      setLoadingMode(null);
    }
  };

  useEffect(() => {
    runDoctor();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const copyReport = async () => {
    if (!results) return;
    const copied = await copyTextToClipboard(JSON.stringify(results, null, 2));
    if (!copied) {
      showToast(t('common.copyFailed'), 'error');
    }
  };

  const summary = results?.summary || { pass: 0, warn: 0, fail: 0 };
  const totalChecks = (summary.pass || 0) + (summary.warn || 0) + (summary.fail || 0);

  return (
    <div className={clsx('flex w-full flex-col gap-5', isPage && 'h-full')}>
      {/* diagHero */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-mint/30 bg-surface-2 px-6 py-5 shadow-[0_16px_32px_-8px_rgba(91,255,160,0.078)]">
        <div className="flex min-w-0 items-center gap-3.5">
          <div className="flex size-[42px] shrink-0 items-center justify-center rounded-full border border-mint/35 bg-mint/[0.08] shadow-[0_0_16px_-4px_rgba(91,255,160,0.44)]">
            <Activity className="size-5 text-mint" strokeWidth={2.25} />
          </div>
          <div className="flex min-w-0 flex-col gap-0.5">
            <h2 className="text-[16px] font-semibold tracking-[-0.2px] text-foreground">
              {t(titleKey)}
            </h2>
            <p className="text-[12px] text-muted">
              {results?.ok
                ? t('dashboard.metricHealthHealthy')
                : t('dashboard.metricHealthIssues')}
              {totalChecks > 0 && ` · ${summary.pass}/${totalChecks}`}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={logsPath}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-foreground/[0.04] px-3 text-[12px] font-medium text-foreground transition hover:border-border-strong"
          >
            <FileText className="size-3.5" />
            {t('common.viewLogs')}
          </Link>
          {results && (
            <Button
              type="button"
              variant="secondary"
              size="xs"
              onClick={() => void copyReport()}
            >
              <Copy className="size-3.5" />
              {t('doctor.copyReport')}
            </Button>
          )}
          <Button
            type="button"
            variant="secondary"
            size="xs"
            onClick={() => void runDoctor(true)}
            disabled={loadingMode !== null}
          >
            <RefreshCw
              className={clsx('size-3.5', loadingMode === 'deep' && 'animate-spin')}
              strokeWidth={2.5}
            />
            {t('doctor.runDeepChecks')}
          </Button>
          <Button
            type="button"
            variant="brand"
            size="xs"
            onClick={() => void runDoctor(false)}
            disabled={loadingMode !== null}
          >
            <RefreshCw
              className={clsx('size-3.5', loadingMode === 'fast' && 'animate-spin')}
              strokeWidth={2.5}
            />
            {t('doctor.runChecks')}
          </Button>
        </div>
      </div>

      {results && (
        <div className="flex flex-col gap-4">
          {/* Summary stat strip */}
          <div className="grid grid-cols-3 gap-3">
            <SummaryTile
              tone="success"
              label={t('doctor.passed')}
              value={summary.pass || 0}
              icon={<CheckCircle className="size-4" />}
            />
            <SummaryTile
              tone="warning"
              label={t('doctor.warnings')}
              value={summary.warn || 0}
              icon={<AlertTriangle className="size-4" />}
            />
            <SummaryTile
              tone="danger"
              label={t('doctor.failed')}
              value={summary.fail || 0}
              icon={<XCircle className="size-4" />}
            />
          </div>

          {results.groups?.map((group: any, index: number) => {
            const groupTotal = group.items?.length || 0;
            const groupPass = (group.items || []).filter((i: any) => i.status === 'pass').length;
            return (
              <div
                key={index}
                className="flex flex-col overflow-hidden rounded-xl border border-border bg-background"
              >
                <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3.5">
                  <h3 className="font-mono text-[11px] font-bold uppercase tracking-[0.16em] text-muted">
                    {group.name}
                  </h3>
                  <span
                    className={clsx(
                      'font-mono text-[10px] font-bold',
                      groupPass === groupTotal ? 'text-mint' : 'text-gold'
                    )}
                  >
                    {groupPass} / {groupTotal}
                  </span>
                </div>
                <div className="flex flex-col">
                  {group.items.map((item: any, j: number) => {
                    const tone = STATUS_TONE[(item.status as CheckStatus) || 'pass'];
                    return (
                      <div
                        key={j}
                        className={clsx(
                          'flex items-start gap-3 border-b border-border px-5 py-2.5 last:border-b-0',
                          tone.row
                        )}
                      >
                        <div className={clsx('mt-0.5 shrink-0', tone.icon)}>{tone.iconNode}</div>
                        <div className="flex min-w-0 flex-1 flex-col gap-1">
                          <div className={clsx('text-[12px]', tone.text)}>{item.message}</div>
                          {item.action && (
                            <div className="cursor-pointer text-[11px] text-cyan underline-offset-2 hover:underline">
                              {item.action}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

const SummaryTile: React.FC<{
  tone: 'success' | 'warning' | 'danger';
  label: string;
  value: number;
  icon: React.ReactNode;
}> = ({ tone, label, value, icon }) => {
  const cfg = {
    success: 'border-mint/30 bg-mint/[0.06] text-mint',
    warning: 'border-gold/30 bg-gold/[0.06] text-gold',
    danger: 'border-danger/30 bg-danger/[0.06] text-danger',
  }[tone];
  return (
    <div className={clsx('flex items-center gap-3 rounded-xl border px-4 py-3', cfg)}>
      <div className="shrink-0">{icon}</div>
      <div className="flex min-w-0 flex-col">
        <div className="text-[20px] font-bold leading-none tracking-[-0.3px]">{value}</div>
        <div className="text-[11px] font-medium uppercase tracking-[0.12em] opacity-80">
          {label}
        </div>
      </div>
    </div>
  );
};
