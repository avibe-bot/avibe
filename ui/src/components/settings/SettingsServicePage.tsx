import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, Globe2, Play, RotateCw, Server, Square, Tag } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useStatus } from '@/context/StatusContext';
import { useApi } from '@/context/ApiContext';
import { apiFetch } from '@/lib/apiFetch';
import { SettingsPageShell } from './SettingsPageShell';
import { CompactField, SettingsPanel, SettingsRow } from './SettingsPrimitives';
import { Button } from '@/components/ui/button';
import { applyAppTitle } from '@/lib/documentTitle';

// Mirrors design.pen mHUcm (VR/CM/Service): two cards.
// svcSec1: header [16, 20] + value rows [12, 20] with bottom borders.
// svcSec2: single row card with title + value pill (read-only mono).
export const SettingsServicePage: React.FC = () => {
  const { t } = useTranslation();
  const { status, control } = useStatus();
  const api = useApi();
  const navigate = useNavigate();
  // Back-compat: Remote Access moved to its own page. Old deep links to the
  // former in-page anchor (#remote-access) land here, so forward them.
  useEffect(() => {
    if (window.location.hash === '#remote-access') {
      navigate('/admin/remote-access', { replace: true });
    }
  }, [navigate]);
  const [config, setConfig] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [uiSaving, setUiSaving] = useState(false);
  const [uiMessage, setUiMessage] = useState<string | null>(null);
  const [nameSaving, setNameSaving] = useState(false);
  const [nameMessage, setNameMessage] = useState<string | null>(null);

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, [api]);

  const isRunning = status.state === 'running';

  const handleAction = async (action: string) => {
    setLoading(true);
    try {
      await control(action);
    } catch (e) {
      console.error('Service control action failed', e);
    } finally {
      setLoading(false);
    }
  };

  const handleUiSaveRestart = async () => {
    if (!config) return;
    setUiSaving(true);
    setUiMessage(null);
    try {
      const uiPayload = {
        setup_host: config.ui?.setup_host || '127.0.0.1',
        setup_port: config.ui?.setup_port || 5123,
      };
      // Send ONLY host/port — not the whole config.ui. Spreading config.ui
      // would persist any unsaved instance_name draft the user typed but
      // didn't Save on that row. The backend deep-merges config saves, so
      // instance_name and chat_message_font_size are preserved regardless.
      await api.saveConfig({ ui: uiPayload });
      await apiFetch('/api/ui/reload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host: uiPayload.setup_host, port: uiPayload.setup_port }),
      });
      // If the UI server's bind host or port changed, the current page is on
      // the old origin and may become unreachable. Redirect (or surface the
      // new origin) so the user is not stranded.
      // Normalize hosts for comparison (strip brackets), but keep an
      // URL-authority form (bracketed for IPv6) for building newOrigin.
      const stripBrackets = (host: string) =>
        host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host;
      const formatAuthorityHost = (host: string) =>
        host.includes(':') ? `[${host}]` : host;
      const currentHostname = stripBrackets(window.location.hostname);
      const currentPort =
        window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
      const targetPort = String(uiPayload.setup_port);
      const normalizedSetupHost = stripBrackets(uiPayload.setup_host);
      const isBindAll =
        normalizedSetupHost === '0.0.0.0' ||
        normalizedSetupHost === '::' ||
        normalizedSetupHost === '';
      const targetHostname = isBindAll ? currentHostname : normalizedSetupHost;
      // The UI server (vibe/ui_server.run_ui_server) binds plain HTTP — any
      // HTTPS access the user has goes through an external TLS proxy whose
      // upstream is this HTTP bind. Always target http:// so the redirect or
      // surfaced URL points at something the rebound server can actually
      // serve, instead of inheriting the current page protocol.
      const newOrigin = `http://${formatAuthorityHost(targetHostname)}:${targetPort}`;
      const originChanged = targetHostname !== currentHostname || targetPort !== currentPort;
      if (originChanged) {
        // Only auto-redirect when the new hostname is something the current
        // browser can reasonably reach. If the user changed the bind to a
        // loopback-only host while accessing remotely, surface the new URL
        // instead of bouncing them somewhere they cannot reach.
        const targetIsLoopback =
          targetHostname === '127.0.0.1' ||
          targetHostname === '::1' ||
          targetHostname === 'localhost';
        const browserOnLoopback =
          currentHostname === '127.0.0.1' ||
          currentHostname === '::1' ||
          currentHostname === 'localhost';
        if (!targetIsLoopback || browserOnLoopback) {
          setUiMessage(t('settings.consoleServerRedirecting', { origin: newOrigin }));
          // Give the UI server a moment to actually rebind before we navigate.
          window.setTimeout(() => {
            window.location.replace(newOrigin);
          }, 1500);
          return;
        }
        setUiMessage(t('settings.consoleServerRelocated', { origin: newOrigin }));
        return;
      }
      setUiMessage(t('dashboard.uiRestartMessage'));
    } catch {
      setUiMessage(t('common.saveFailed'));
    } finally {
      setUiSaving(false);
    }
  };

  // The instance name only changes the browser tab title ("Avibe - <name>"),
  // so it saves config without restarting the UI server and applies the new
  // title immediately for live feedback.
  const handleSaveInstanceName = async () => {
    if (!config) return;
    setNameSaving(true);
    setNameMessage(null);
    try {
      const instanceName = (config.ui?.instance_name || '').trim();
      // Persist ONLY the instance name. Spreading the shared (and possibly
      // dirty) config.ui would also save unsaved Console server host/port
      // edits — but without the /api/ui/reload + redirect that
      // handleUiSaveRestart performs — so the running UI keeps the old bind
      // while the next restart silently moves to the unsaved address. The
      // backend deep-merges config saves, so host/port are preserved.
      await api.saveConfig({ ui: { instance_name: instanceName } });
      // Reflect the new title immediately. system_hostname is the read-only,
      // server-computed fallback (unaffected by host/port edits).
      applyAppTitle({ ui: { instance_name: instanceName, system_hostname: config.ui?.system_hostname } });
      setNameMessage(t('common.saved'));
    } catch {
      setNameMessage(t('common.saveFailed'));
    } finally {
      setNameSaving(false);
    }
  };

  return (
    <SettingsPageShell
      activeTab="service"
      title={t('settings.serviceTitle')}
      subtitle={t('settings.serviceSubtitle')}
    >

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Server className="size-3.5 text-mint" />
            {t('settings.serviceRuntimeTitle')}
          </span>
        }
        description={t('settings.serviceRuntimeSubtitle')}
        actions={
          <span
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em]',
              isRunning
                ? 'border-mint/30 bg-mint/[0.08] text-mint'
                : 'border-border bg-foreground/[0.04] text-muted'
            )}
          >
            <span
              className={clsx(
                'size-1.5 rounded-full',
                isRunning ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.9)]' : 'bg-muted'
              )}
            />
            {isRunning ? t('common.running') : t('common.stopped')}
          </span>
        }
      >
        <SettingsRow
          title={t('settings.statusNow')}
          description={`PID ${status.service_pid || status.pid || '-'}`}
          control={
            <div className="flex flex-wrap gap-2">
              {!isRunning && (
                <Button
                  type="button"
                  variant="brand"
                  size="xs"
                  onClick={() => void handleAction('start')}
                  disabled={loading}
                >
                  <Play className="size-3.5" strokeWidth={2.5} />
                  {t('common.start')}
                </Button>
              )}
              {isRunning && (
                <Button
                  type="button"
                  variant="secondary"
                  size="xs"
                  onClick={() => void handleAction('stop')}
                  disabled={loading}
                >
                  <Square className="size-3.5" strokeWidth={2.5} />
                  {t('common.stop')}
                </Button>
              )}
              <Button
                type="button"
                variant="secondary"
                size="xs"
                onClick={() => void handleAction('restart')}
                disabled={loading}
              >
                <RotateCw className="size-3.5" strokeWidth={2.5} />
                {t('common.restart')}
              </Button>
            </div>
          }
        />
        <SettingsRow
          title={
            <span className="inline-flex items-center gap-2">
              <Globe2 className="size-3.5 text-cyan" />
              {t('settings.consoleServerTitle')}
            </span>
          }
          description={uiMessage || t('settings.consoleServerHint')}
          control={
            <div className="grid grid-cols-[120px_96px_auto] items-center gap-2">
              <CompactField
                aria-label={t('dashboard.host')}
                value={config?.ui?.setup_host || '127.0.0.1'}
                onChange={(event) => {
                  const host = event.target.value || '127.0.0.1';
                  setUiMessage(null);
                  setConfig((prev: any) => ({
                    ...(prev || {}),
                    ui: { ...((prev && prev.ui) || {}), setup_host: host },
                  }));
                }}
              />
              <CompactField
                aria-label={t('dashboard.port')}
                type="number"
                min={1024}
                max={65535}
                value={config?.ui?.setup_port || 5123}
                onChange={(event) => {
                  const port = Number(event.target.value) || 5123;
                  setUiMessage(null);
                  setConfig((prev: any) => ({
                    ...(prev || {}),
                    ui: { ...((prev && prev.ui) || {}), setup_port: port },
                  }));
                }}
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => void handleUiSaveRestart()}
                disabled={uiSaving}
                className="text-[12px]"
              >
                <RotateCw className={clsx('size-3.5', uiSaving && 'animate-spin')} strokeWidth={2.5} />
                {uiSaving ? t('common.saving') : t('common.saveAndRestart')}
              </Button>
            </div>
          }
        />
        <SettingsRow
          title={
            <span className="inline-flex items-center gap-2">
              <Tag className="size-3.5 text-cyan" />
              {t('settings.instanceNameTitle')}
            </span>
          }
          description={nameMessage || t('settings.instanceNameHint')}
          control={
            <div className="grid grid-cols-[minmax(160px,220px)_auto] items-center gap-2">
              <CompactField
                aria-label={t('settings.instanceNameTitle')}
                placeholder={config?.ui?.system_hostname || ''}
                value={config?.ui?.instance_name || ''}
                onChange={(event) => {
                  const instanceName = event.target.value;
                  setNameMessage(null);
                  setConfig((prev: any) => ({
                    ...(prev || {}),
                    ui: { ...((prev && prev.ui) || {}), instance_name: instanceName },
                  }));
                }}
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => void handleSaveInstanceName()}
                disabled={nameSaving}
                className="text-[12px]"
              >
                {nameSaving ? t('common.saving') : t('common.save')}
              </Button>
            </div>
          }
        />
      </SettingsPanel>

      {/* Mirrors design.pen CuVKM (svcSec2): single-row read-only value pill */}
      <SettingsPanel>
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
          <div className="text-[13px] font-medium text-foreground">{t('settings.logFileLabel')}</div>
          <div className="inline-flex items-center gap-2 rounded-lg border border-border bg-foreground/[0.04] px-3 py-2">
            <FileText className="size-3.5 text-muted" />
            <span className="font-mono text-[11px] text-foreground">~/.avibe/logs/vibe_remote.log</span>
          </div>
        </div>
      </SettingsPanel>
    </SettingsPageShell>
  );
};
