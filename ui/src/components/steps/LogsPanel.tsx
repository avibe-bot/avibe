import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown,
  FileText,
  Filter,
  Pause,
  Play,
  RefreshCw,
  Search,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi, type LogEntry, type LogSource } from '../../context/ApiContext';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Select } from '../ui/select';

type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'ALL';

const LEVEL_PILL: Record<string, string> = {
  DEBUG: 'border-border bg-foreground/[0.04] text-muted',
  INFO: 'border-cyan/30 bg-cyan/[0.08] text-cyan-ink',
  WARNING: 'border-gold/30 bg-gold/[0.08] text-gold-ink',
  ERROR: 'border-danger/30 bg-danger/[0.08] text-danger',
};

const parseLogMessage = (message: string): { location?: string; content: string } => {
  const match = message.match(/^\[([^\]]+)\]\s*-?\s*(.*)$/s);
  if (match) return { location: match[1], content: match[2] };
  return { content: message };
};

interface LogsPanelProps {
  titleKey?: string;
  compactHeader?: boolean;
}

export const LogsPanel: React.FC<LogsPanelProps> = ({
  titleKey = 'logs.title',
  compactHeader = false,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [sources, setSources] = useState<LogSource[]>([]);
  const [selectedSource, setSelectedSource] = useState('all');
  const [loading, setLoading] = useState(false);
  const [logsTotal, setLogsTotal] = useState(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<LogLevel>('ALL');
  const [autoRefresh, setAutoRefresh] = useState(false);
  const logsContainerRef = useRef<HTMLDivElement>(null);
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadLogs = async (source = selectedSource) => {
    setLoading(true);
    try {
      const res = await api.getLogs(2000, source);
      setLogs(res.logs);
      setLogsTotal(res.total);
      setSources(res.sources);
      setSelectedSource(res.source);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadLogs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (autoRefresh) {
      autoRefreshRef.current = setInterval(() => {
        void loadLogs(selectedSource);
      }, 5000);
    } else if (autoRefreshRef.current) {
      clearInterval(autoRefreshRef.current);
      autoRefreshRef.current = null;
    }
    return () => {
      if (autoRefreshRef.current) {
        clearInterval(autoRefreshRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, selectedSource]);

  useEffect(() => {
    if (logsContainerRef.current) {
      logsContainerRef.current.scrollTop = 0;
    }
  }, [logs]);

  const filteredLogs = useMemo(
    () =>
      logs.filter((log) => {
        const matchesLevel = levelFilter === 'ALL' || log.level === levelFilter;
        const matchesSearch =
          !searchQuery ||
          log.message.toLowerCase().includes(searchQuery.toLowerCase()) ||
          log.logger.toLowerCase().includes(searchQuery.toLowerCase()) ||
          log.timestamp.includes(searchQuery);
        return matchesLevel && matchesSearch;
      }),
    [logs, levelFilter, searchQuery]
  );

  const levelCounts = useMemo(() => {
    const counts: Record<string, number> = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0 };
    logs.forEach((log) => {
      if (counts[log.level] !== undefined) counts[log.level]++;
    });
    return counts;
  }, [logs]);

  const selectedSourceMeta = useMemo(
    () => sources.find((source) => source.key === selectedSource) ?? null,
    [selectedSource, sources]
  );

  const getSourceLabel = (sourceKey: string) => {
    switch (sourceKey) {
      case 'all':
        return t('logs.sources.all');
      case 'service':
        return t('logs.sources.service');
      case 'service_stdout':
        return t('logs.sources.serviceStdout');
      case 'service_stderr':
        return t('logs.sources.serviceStderr');
      case 'ui_stdout':
        return t('logs.sources.uiStdout');
      case 'ui_stderr':
        return t('logs.sources.uiStderr');
      default:
        return sourceKey;
    }
  };

  const scrollToTop = () => {
    if (logsContainerRef.current) logsContainerRef.current.scrollTop = 0;
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {!compactHeader && (
          <h2 className="inline-flex items-center gap-2 text-[24px] font-bold tracking-[-0.3px] text-foreground">
            <FileText className="size-5 text-cyan-ink" />
            {t(titleKey)}
          </h2>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setAutoRefresh(!autoRefresh)}
            className={clsx(
              'inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-[12px] font-medium transition',
              autoRefresh
                ? 'border-cyan/40 bg-cyan/[0.08] text-cyan-ink'
                : 'border-border bg-foreground/[0.04] text-foreground hover:border-border-strong'
            )}
          >
            {autoRefresh ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
            {autoRefresh ? t('logs.autoRefreshOn') : t('logs.autoRefresh')}
          </button>
          <Button
            type="button"
            variant="brand"
            size="xs"
            className="h-9"
            onClick={() => void loadLogs(selectedSource)}
            disabled={loading}
          >
            <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} strokeWidth={2.5} />
            {t('common.refresh')}
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-[220px] flex-1">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <Input
            type="text"
            placeholder={t('logs.searchPlaceholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 text-[12px]"
          />
        </div>
        <div className="flex items-center gap-2">
          <Filter className="size-3.5 text-muted" />
          <Select
            value={levelFilter}
            onChange={(e) => setLevelFilter(e.target.value as LogLevel)}
            className="text-[12px]"
            wrapperClassName="w-auto"
          >
            <option value="ALL">{t('logs.allLevels')}</option>
            <option value="ERROR">{t('logs.error')} ({levelCounts.ERROR})</option>
            <option value="WARNING">{t('logs.warning')} ({levelCounts.WARNING})</option>
            <option value="INFO">{t('logs.info')} ({levelCounts.INFO})</option>
            <option value="DEBUG">{t('logs.debug')} ({levelCounts.DEBUG})</option>
          </Select>
        </div>
      </div>

      {sources.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {sources.map((source) => (
            <button
              key={source.key}
              type="button"
              onClick={() => void loadLogs(source.key)}
              className={clsx(
                'inline-flex h-7 items-center gap-1 rounded-full border px-3 text-[11px] font-medium transition-colors',
                selectedSource === source.key
                  ? 'border-mint/35 bg-mint/[0.08] text-mint-ink shadow-[0_0_12px_-4px_rgba(91,255,160,0.5)]'
                  : 'border-border bg-foreground/[0.04] text-muted hover:border-border-strong hover:text-foreground'
              )}
            >
              {getSourceLabel(source.key)}
              {source.total > 0 && (
                <span className="font-mono text-[10px] opacity-70">{source.total}</span>
              )}
            </button>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {(['ALL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'] as LogLevel[]).map((level) => (
          <button
            key={level}
            type="button"
            onClick={() => setLevelFilter(level)}
            className={clsx(
              'inline-flex h-7 items-center gap-1 rounded-full border px-3 text-[11px] font-medium transition-colors',
              levelFilter === level
                ? 'border-cyan/40 bg-cyan/[0.08] text-cyan-ink'
                : 'border-border bg-foreground/[0.04] text-muted hover:border-border-strong hover:text-foreground'
            )}
          >
            {level === 'ALL' ? t('logs.all') : level}
            {level !== 'ALL' && (
              <span className="font-mono text-[10px] opacity-70">{levelCounts[level]}</span>
            )}
          </button>
        ))}
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-background">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border bg-foreground/[0.02] px-4 py-2.5">
          <span className="font-mono text-[11px] font-medium text-muted">
            {t('logs.entriesCount', { filtered: filteredLogs.length, total: logs.length })}
            {logsTotal > logs.length && ` · ${t('logs.totalInFile', { total: logsTotal })}`}
          </span>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={scrollToTop}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-cyan-ink transition hover:text-cyan-ink/80"
            >
              <ArrowDown className="size-3" /> {t('logs.jumpToLatest')}
            </button>
            <code className="rounded border border-border bg-background px-2 py-0.5 font-mono text-[10px] text-muted">
              {selectedSourceMeta?.path || t('logs.allFiles')}
            </code>
          </div>
        </div>

        {loading && logs.length === 0 ? (
          <div className="flex flex-1 items-center justify-center p-8 text-[12px] text-muted">
            <div className="flex flex-col items-center gap-2">
              <RefreshCw className="size-5 animate-spin" />
              {t('logs.loadingLogs')}
            </div>
          </div>
        ) : filteredLogs.length === 0 ? (
          <div className="flex flex-1 items-center justify-center p-8 text-[12px] text-muted">
            {logs.length === 0 ? t('logs.noLogsAvailable') : t('logs.noLogsMatch')}
          </div>
        ) : (
          <div
            ref={logsContainerRef}
            className="flex-1 divide-y divide-border overflow-y-auto"
            style={{ minHeight: '500px', maxHeight: 'calc(100vh - 380px)' }}
          >
            {filteredLogs
              .slice()
              .reverse()
              .map((log, i) => {
                const { location, content } = parseLogMessage(log.message);
                return (
                  <div
                    key={i}
                    className={clsx(
                      'px-4 py-2.5 transition-colors hover:bg-foreground/[0.02]',
                      log.level === 'ERROR' && 'bg-danger/[0.04]',
                      log.level === 'WARNING' && 'bg-gold/[0.04]'
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[10px] text-muted">{log.timestamp}</span>
                      <span
                        className={clsx(
                          'inline-block min-w-[60px] rounded border px-1.5 py-0.5 text-center font-mono text-[10px] font-bold',
                          LEVEL_PILL[log.level] || LEVEL_PILL.INFO
                        )}
                      >
                        {log.level}
                      </span>
                      <span
                        className="max-w-[220px] truncate font-mono text-[10px] text-muted"
                        title={log.logger}
                      >
                        {log.logger}
                      </span>
                      <span className="font-mono text-[10px] text-muted/70">
                        {getSourceLabel(log.source)}
                      </span>
                      {location && (
                        <span className="font-mono text-[10px] text-muted/70">[{location}]</span>
                      )}
                    </div>
                    <div
                      className={clsx(
                        'mt-1.5 whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed',
                        log.level === 'ERROR'
                          ? 'text-danger'
                          : log.level === 'WARNING'
                            ? 'text-gold-ink'
                            : 'text-foreground'
                      )}
                    >
                      {content}
                    </div>
                  </div>
                );
              })}
          </div>
        )}
      </div>
    </div>
  );
};
