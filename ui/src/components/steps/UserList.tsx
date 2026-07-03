import React, { useEffect, useMemo, useState } from 'react';
import {
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  KeyRound,
  Plus,
  RefreshCw,
  Shield,
  Trash2,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { getEnabledPlatforms, platformSupportsToolcallDelivery } from '../../lib/platforms';
import { DirectoryBrowser } from '../ui/directory-browser';
import { copyTextToClipboard } from '../../lib/utils';
import { PlatformIcon } from '../visual';
import { RoutingConfigPanel } from '../shared/RoutingConfigPanel';
import { SearchField, ToggleSwitch } from '../settings/SettingsPrimitives';
import { Button } from '../ui/button';
import { Input } from '../ui/input';

interface UserConfig {
  display_name: string;
  is_admin: boolean;
  bound_at: string;
  enabled: boolean;
  show_message_types: string[];
  custom_cwd: string;
  routing: {
    agent_name?: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    opencode_agent?: string | null;
    opencode_model?: string | null;
    opencode_reasoning_effort?: string | null;
    claude_agent?: string | null;
    claude_model?: string | null;
    claude_reasoning_effort?: string | null;
    codex_agent?: string | null;
    codex_model?: string | null;
    codex_reasoning_effort?: string | null;
  };
}

interface BindCodeItem {
  code: string;
  type: string;
  created_at: string;
  expires_at: string | null;
  is_active: boolean;
  used_by: string[];
}

interface AggregatedUser {
  key: string; // `${platform}::${userId}`
  platform: string;
  userId: string;
  config: UserConfig;
}

const AVATAR_TONES = [
  { textCls: 'text-mint', bg: 'rgba(91,255,160,0.12)', border: 'rgba(91,255,160,0.33)' },
  { textCls: 'text-cyan', bg: 'rgba(63,224,229,0.12)', border: 'rgba(63,224,229,0.33)' },
  { textCls: 'text-violet', bg: 'rgba(124,91,255,0.12)', border: 'rgba(124,91,255,0.33)' },
  { textCls: 'text-gold', bg: 'rgba(255,200,87,0.12)', border: 'rgba(255,200,87,0.33)' },
];

const hashCode = (s: string) => {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
};

const getInitials = (name: string, fallback: string): string => {
  const trimmed = (name || '').trim();
  if (!trimmed) return (fallback || '??').slice(0, 2).toUpperCase();
  const parts = trimmed.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  const onlyAlpha = trimmed.replace(/[^a-zA-Z]/g, '');
  return (onlyAlpha || trimmed).slice(0, 2).toUpperCase();
};

const displayNameForUser = (u: AggregatedUser): string => {
  const name = (u.config.display_name || '').trim();
  return name || u.userId;
};

const formatExpiry = (expiresAt: string | null): string => {
  if (!expiresAt) return '';
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return '00:00';
  const totalSeconds = Math.floor(ms / 1000);
  const days = Math.floor(totalSeconds / 86_400);
  if (days > 0) return `${days}d`;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
};

// ─── Bind Code Section ───────────────────────────────────────────────────

interface BindCodeCardProps {
  refreshTrigger: number;
  onCodesChanged: () => void;
}

const BindCodeCard: React.FC<BindCodeCardProps> = ({ refreshTrigger, onCodesChanged }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [codes, setCodes] = useState<BindCodeItem[]>([]);
  const [showFormModal, setShowFormModal] = useState(false);
  const [showAllModal, setShowAllModal] = useState(false);
  const [newType, setNewType] = useState<'one_time' | 'expiring'>('one_time');
  const [newExpiry, setNewExpiry] = useState('');
  const [copiedCode, setCopiedCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  const loadCodes = async () => {
    try {
      const result = await api.getBindCodes();
      if (result.ok) setCodes(result.bind_codes || []);
    } catch (e) {
      console.error('Failed to load bind codes:', e);
    }
  };

  useEffect(() => { loadCodes(); }, [refreshTrigger]);

  // Tick every second to refresh the countdown for expiring codes
  useEffect(() => {
    const id = setInterval(() => setTick((v) => v + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const getCodeStatus = (bc: BindCodeItem) => {
    if (!bc.is_active) return bc.type === 'one_time' && bc.used_by.length > 0 ? 'used' : 'inactive';
    if (bc.type === 'expiring' && bc.expires_at && new Date(bc.expires_at) < new Date()) return 'expired';
    return 'active';
  };

  const activeCodes = useMemo(() => codes.filter((bc) => getCodeStatus(bc) === 'active'), [codes, tick]);
  const otherCodes = useMemo(() => codes.filter((bc) => getCodeStatus(bc) !== 'active'), [codes, tick]);
  const primary = activeCodes[0];
  const moreCount = Math.max(0, activeCodes.length - 1);

  const handleCreate = async () => {
    setLoading(true);
    try {
      const result = await api.createBindCode(newType, newType === 'expiring' ? newExpiry : undefined);
      if (result.ok) {
        showToast(t('bindCode.created'));
        setShowFormModal(false);
        setNewType('one_time');
        setNewExpiry('');
        loadCodes();
        onCodesChanged();
      }
    } catch (e) {
      console.error('Failed to create bind code:', e);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (code: string) => {
    try {
      const result = await api.deleteBindCode(code);
      if (result.ok) {
        showToast(t('bindCode.deleted'));
        loadCodes();
        onCodesChanged();
      }
    } catch (e) {
      console.error('Failed to delete bind code:', e);
    }
  };

  const handleCopy = async (code: string) => {
    const copied = await copyTextToClipboard(`bind ${code}`);
    if (!copied) {
      showToast(t('common.copyFailed'), 'error');
      return;
    }
    setCopiedCode(code);
    setTimeout(() => setCopiedCode(null), 2000);
  };

  return (
    <>
      <div
        className="flex flex-col gap-5 rounded-2xl border bg-surface-2/40 p-5 shadow-[0_24px_48px_-30px_rgba(91,255,160,0.20)] md:flex-row md:flex-wrap md:items-stretch md:justify-between md:gap-6 md:p-6"
        style={{ borderColor: 'rgba(91,255,160,0.33)' }}
      >
        {/* Actions area — appears first on mobile, right on desktop */}
        <div className="-order-1 flex flex-col items-stretch gap-3 md:order-none md:shrink-0 md:items-end">
          {primary ? (
            <ActiveCodeBox
              code={primary}
              onCopy={() => handleCopy(primary.code)}
              copied={copiedCode === primary.code}
              t={t}
            />
          ) : (
            <span className="inline-flex items-center gap-1.5 font-mono text-[10px] font-bold tracking-[0.12em] text-muted">
              <span className="size-1.5 rounded-full bg-muted" />
              {t('bindCode.noActiveCode')}
            </span>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {moreCount > 0 || otherCodes.length > 0 ? (
              <Button
                type="button"
                variant="secondary"
                size="xs"
                onClick={() => setShowAllModal(true)}
                className="text-muted hover:text-foreground"
              >
                {moreCount > 0
                  ? t('bindCode.viewMore', { count: moreCount })
                  : t('bindCode.viewUnavailable', { count: otherCodes.length })}
              </Button>
            ) : null}
            {primary && (
              <Button
                type="button"
                variant="secondary"
                size="xs"
                onClick={() => handleDelete(primary.code)}
                className="text-muted hover:border-danger/40 hover:text-danger"
              >
                {t('bindCode.revoke')}
              </Button>
            )}
            <Button type="button" variant="brand" size="xs" onClick={() => setShowFormModal(true)}>
              <Plus size={14} strokeWidth={2.4} />
              {primary ? t('bindCode.newCode') : t('bindCode.createCode')}
            </Button>
          </div>
        </div>

        {/* Invite copy — appears below actions on mobile, left on desktop */}
        <div className="flex min-w-0 flex-col gap-2.5 md:flex-1">
          <span className="inline-flex w-max items-center gap-1.5 rounded-full border border-mint/40 bg-mint-soft px-2.5 py-1 font-mono text-[10px] font-bold tracking-[0.12em] text-mint">
            <KeyRound size={11} strokeWidth={2.4} />
            {t('bindCode.badge')}
          </span>
          <h3 className="text-[18px] font-bold leading-tight tracking-[-0.3px] text-foreground">
            {t('bindCode.inviteTitle')}
          </h3>
          <p className="max-w-[560px] text-[13px] leading-[1.55] text-muted">
            {t('bindCode.inviteDescription')}
          </p>
        </div>
      </div>

      {showFormModal && (
        <Modal title={t('bindCode.newCode')} onClose={() => setShowFormModal(false)}>
          <div className="space-y-4 p-4 text-sm">
            <div className="flex items-center gap-4">
              <span className="text-muted">{t('bindCode.codeType')}</span>
              <label className="flex items-center gap-1.5 text-foreground">
                <input
                  type="radio"
                  checked={newType === 'one_time'}
                  onChange={() => setNewType('one_time')}
                  className="text-mint"
                />
                {t('bindCode.oneTime')}
              </label>
              <label className="flex items-center gap-1.5 text-foreground">
                <input
                  type="radio"
                  checked={newType === 'expiring'}
                  onChange={() => setNewType('expiring')}
                  className="text-mint"
                />
                {t('bindCode.expiring')}
              </label>
            </div>
            {newType === 'expiring' && (
              <div className="flex items-center gap-3">
                <label className="text-muted">{t('bindCode.expirationDate')}</label>
                <Input
                  type="date"
                  value={newExpiry}
                  onChange={(e) => setNewExpiry(e.target.value)}
                  min={new Date().toISOString().split('T')[0]}
                />
              </div>
            )}
            <Button
              type="button"
              variant="brand"
              size="xs"
              onClick={handleCreate}
              disabled={loading || (newType === 'expiring' && !newExpiry)}
            >
              {t('bindCode.generate')}
            </Button>
          </div>
        </Modal>
      )}

      {showAllModal && (
        <Modal title={t('bindCode.allCodesTitle')} onClose={() => setShowAllModal(false)}>
          <div className="max-h-[60vh] overflow-y-auto p-2">
            {codes.length === 0 ? (
              <p className="p-4 text-sm text-muted">{t('bindCode.noCodes')}</p>
            ) : (
              <div className="divide-y divide-border">
                {codes.map((bc) => {
                  const status = getCodeStatus(bc);
                  return (
                    <div key={bc.code} className="flex flex-wrap items-center justify-between gap-3 px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="rounded border border-border bg-surface px-2 py-0.5 font-mono text-[12px] text-foreground">bind {bc.code}</code>
                        <span
                          className={clsx(
                            'rounded-full border px-2 py-0.5 text-[10px] font-mono font-bold tracking-[0.12em]',
                            status === 'active' && 'border-mint/40 bg-mint-soft text-mint',
                            status === 'used' && 'border-border bg-foreground/[0.04] text-muted',
                            status === 'expired' && 'border-gold/40 bg-gold/10 text-gold',
                            status === 'inactive' && 'border-border bg-foreground/[0.04] text-muted',
                          )}
                        >
                          {t(`bindCode.${status}`)}
                        </span>
                        <span className="text-[11px] text-muted">
                          {bc.type === 'one_time' ? t('bindCode.oneTime') : t('bindCode.expiring')}
                        </span>
                        {bc.used_by.length > 0 && (
                          <span className="text-[11px] text-muted">{t('bindCode.usedBy', { count: bc.used_by.length })}</span>
                        )}
                        {bc.expires_at && (
                          <span className="text-[11px] text-muted">
                            {t('bindCode.expiresAt', { date: new Date(bc.expires_at).toLocaleDateString() })}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => handleCopy(bc.code)}
                          title={t('bindCode.copy')}
                          className="rounded p-1.5 text-muted transition-colors hover:text-foreground"
                        >
                          {copiedCode === bc.code ? <Check size={14} className="text-mint" /> : <Copy size={14} />}
                        </button>
                        {bc.is_active && (
                          <button
                            type="button"
                            onClick={() => handleDelete(bc.code)}
                            title={t('bindCode.delete')}
                            className="rounded p-1.5 text-muted transition-colors hover:text-danger"
                          >
                            <Trash2 size={14} />
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </Modal>
      )}
    </>
  );
};

const ActiveCodeBox: React.FC<{
  code: BindCodeItem;
  onCopy: () => void;
  copied: boolean;
  t: (k: string, opts?: any) => string;
}> = ({ code, onCopy, copied, t }) => {
  const expiryLabel = code.type === 'expiring' && code.expires_at
    ? t('bindCode.expiresIn', { time: formatExpiry(code.expires_at) })
    : t('bindCode.noExpiry');

  return (
    <div className="flex flex-col items-stretch gap-1 md:items-end">
      <span className="font-mono text-[10px] font-bold tracking-[0.12em] text-gold">
        {t('bindCode.activeCodeLabel')} · {expiryLabel}
      </span>
      <button
        type="button"
        onClick={onCopy}
        className="inline-flex items-center justify-between gap-2.5 rounded-lg border bg-background px-4 py-2.5 transition-colors hover:brightness-110 md:justify-start"
        style={{ borderColor: 'rgba(255,200,87,0.33)' }}
        title={t('bindCode.copy')}
      >
        <span className="font-mono text-[18px] font-bold tracking-[0.12em] text-gold">bind {code.code}</span>
        {copied ? (
          <Check size={14} className="shrink-0 text-mint" />
        ) : (
          <Copy size={14} className="shrink-0 text-muted" />
        )}
      </button>
    </div>
  );
};

const Modal: React.FC<{ title: string; onClose: () => void; children: React.ReactNode }> = ({ title, onClose, children }) => (
  <div
    className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
    role="dialog"
    aria-modal="true"
    aria-label={title}
    onClick={onClose}
  >
    <div
      className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-surface-2 shadow-[0_42px_80px_-42px_rgba(0,0,0,0.85)]"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h4 className="text-base font-semibold text-foreground">{title}</h4>
        <button
          type="button"
          onClick={onClose}
          className="rounded text-muted transition-colors hover:text-foreground"
          title={title}
        >
          <X size={16} />
        </button>
      </div>
      {children}
    </div>
  </div>
);

// ─── User List Page ──────────────────────────────────────────────────────

export const UserList: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [, setLoading] = useState(false);
  const [usersByPlatform, setUsersByPlatform] = useState<Record<string, Record<string, UserConfig>>>({});
  const [config, setConfig] = useState<any>({});
  const [opencodeOptionsByCwd, setOpencodeOptionsByCwd] = useState<Record<string, any>>({});
  const [claudeAgentsByCwd, setClaudeAgentsByCwd] = useState<Record<string, any[]>>({});
  const [codexAgentsByCwd, setCodexAgentsByCwd] = useState<Record<string, any[]>>({});
  const [claudeModels, setClaudeModels] = useState<string[]>([]);
  const [claudeModelLabels, setClaudeModelLabels] = useState<Record<string, string>>({});
  const [claudeReasoningOptions, setClaudeReasoningOptions] = useState<Record<string, { value: string; label: string }[]>>({});
  const [codexModels, setCodexModels] = useState<string[]>([]);
  const [browsingCwdFor, setBrowsingCwdFor] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [showDisabled, setShowDisabled] = useState(false);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const enabledPlatforms = useMemo(() => getEnabledPlatforms(config), [config]);
  const vibeAgents = config.agent_catalog?.agents || [];
  const defaultAgentName = config.agent_catalog?.default_agent_name || null;
  const agentByName = useMemo(
    () => Object.fromEntries(vibeAgents.map((agent: any) => [agent.name, agent])),
    [vibeAgents]
  );

  useEffect(() => {
    api.getConfig().then(async (loadedConfig) => {
      try {
        const settings = await api.getSettings(loadedConfig.platform);
        setConfig({ ...loadedConfig, agent_catalog: settings.agent_catalog });
      } catch {
        setConfig(loadedConfig);
      }
    });
  }, []);

  const loadAllUsers = async () => {
    if (!enabledPlatforms.length) return;
    const results = await Promise.all(
      enabledPlatforms.map(async (p) => {
        try {
          const r = await api.getUsers(p);
          return [p, r.ok ? (r.users || {}) : {}] as const;
        } catch {
          return [p, {}] as const;
        }
      })
    );
    const next: Record<string, Record<string, UserConfig>> = {};
    for (const [p, u] of results) next[p] = u as Record<string, UserConfig>;
    setUsersByPlatform(next);
  };

  useEffect(() => { loadAllUsers(); }, [enabledPlatforms.join(','), refreshTrigger]);

  const loadOpenCodeOptions = async (cwd: string) => {
    try {
      const result = await api.opencodeOptions(cwd);
      if (result.ok) setOpencodeOptionsByCwd((prev) => ({ ...prev, [cwd]: result.data }));
    } catch (e) { console.error('Failed to load OpenCode options:', e); }
  };

  const loadClaudeAgents = async (cwd: string) => {
    try {
      const result = await api.claudeAgents(cwd);
      if (result.ok) setClaudeAgentsByCwd((prev) => ({ ...prev, [cwd]: result.agents || [] }));
    } catch (e) { console.error('Failed to load Claude agents:', e); }
  };

  const loadCodexAgents = async (cwd: string) => {
    try {
      const result = await api.codexAgents(cwd);
      if (result.ok) setCodexAgentsByCwd((prev) => ({ ...prev, [cwd]: result.agents || [] }));
    } catch (e) { console.error('Failed to load Codex agents:', e); }
  };

  useEffect(() => {
    if (config.agents?.claude?.enabled) {
      api.claudeModels().then((r) => {
        if (r.ok) {
          setClaudeModels(r.models || []);
          setClaudeModelLabels(r.model_labels || {});
          setClaudeReasoningOptions(r.reasoning_options || {});
        }
      });
    }
  }, [config.agents?.claude?.enabled]);

  useEffect(() => {
    if (config.agents?.codex?.enabled) api.codexModels().then(r => r.ok && setCodexModels(r.models || []));
  }, [config.agents?.codex?.enabled]);

  // Aggregate users
  const aggregated = useMemo<AggregatedUser[]>(() => {
    const flat: AggregatedUser[] = [];
    for (const [platform, users] of Object.entries(usersByPlatform)) {
      for (const [userId, cfg] of Object.entries(users)) {
        flat.push({ key: `${platform}::${userId}`, platform, userId, config: cfg });
      }
    }
    return flat.sort(
      (a, b) =>
        Number(b.config.is_admin) - Number(a.config.is_admin) ||
        displayNameForUser(a).localeCompare(displayNameForUser(b)) ||
        a.userId.localeCompare(b.userId)
    );
  }, [usersByPlatform]);

  // Filtered and search-applied list
  const visibleUsers = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return aggregated.filter((u) => {
      if (!showDisabled && !u.config.enabled) return false;
      if (!q) return true;
      const name = (u.config.display_name || '').toLowerCase();
      const id = u.userId.toLowerCase();
      return name.includes(q) || id.includes(q);
    });
  }, [aggregated, searchQuery, showDisabled]);

  // Load agent options for visible enabled users
  useEffect(() => {
    const defaultCwd = config.runtime?.default_cwd || '~/work';
    const defaultAgent = agentByName[defaultAgentName || ''] || null;
    aggregated.forEach((u) => {
      if (!u.config.enabled) return;
      const cwd = u.config.custom_cwd || defaultCwd;
      const routing = u.config.routing || {};
      const selectedAgent = routing.agent_name ? agentByName[routing.agent_name] : null;
      const backend = selectedAgent?.backend || defaultAgent?.backend || 'opencode';
      if (backend === 'opencode' && config.agents?.opencode?.enabled && !opencodeOptionsByCwd[cwd]) loadOpenCodeOptions(cwd);
      if (backend === 'claude' && config.agents?.claude?.enabled && !claudeAgentsByCwd[cwd]) loadClaudeAgents(cwd);
      if (backend === 'codex' && config.agents?.codex?.enabled && !codexAgentsByCwd[cwd]) loadCodexAgents(cwd);
    });
  }, [aggregated, config, agentByName, defaultAgentName]);

  const persistUsers = async (platform: string, next: Record<string, UserConfig>) => {
    setLoading(true);
    try {
      await api.saveUsers({ users: next }, platform);
      showToast(t('userList.settingsSaved'));
    } catch {
      showToast(t('userList.settingsSaveFailed'), 'error');
    } finally {
      setLoading(false);
    }
  };

  const updateUser = (platform: string, userId: string, patch: Partial<UserConfig>) => {
    const platformUsers = usersByPlatform[platform] || {};
    const base = platformUsers[userId] || defaultUserConfig();
    const next = { ...base, ...patch };
    if (!next.routing || typeof next.routing !== 'object') {
      next.routing = { agent_name: null };
    }
    const nextPlatformUsers = { ...platformUsers, [userId]: next };
    setUsersByPlatform((prev) => ({ ...prev, [platform]: nextPlatformUsers }));
    void persistUsers(platform, nextPlatformUsers);
  };

  const handleToggleAdmin = async (platform: string, userId: string, isAdmin: boolean) => {
    const platformUsers = usersByPlatform[platform] || {};
    const current = platformUsers[userId];
    const adminCount = Object.values(platformUsers).filter((u) => u.is_admin).length;
    if (current?.is_admin && !isAdmin && adminCount <= 1) {
      if (!confirm(t('userList.lastAdminDemoteWarning'))) return;
    }
    try {
      const result = await api.toggleAdmin(userId, isAdmin, platform);
      if (result.ok) {
        showToast(t('userList.adminToggled'));
        loadAllUsers();
      } else {
        showToast(result.error || t('userList.cannotRemoveLastAdmin'), 'error');
      }
    } catch (e) {
      console.error('Failed to toggle admin:', e);
    }
  };

  const handleRemoveUser = async (platform: string, userId: string) => {
    const platformUsers = usersByPlatform[platform] || {};
    const current = platformUsers[userId];
    const adminCount = Object.values(platformUsers).filter((u) => u.is_admin).length;
    const warningKey = current?.is_admin && adminCount <= 1 ? 'userList.lastAdminRemoveWarning' : 'userList.removeConfirm';
    if (!confirm(t(warningKey))) return;
    try {
      const result = await api.removeUser(userId, platform);
      if (result.ok) {
        showToast(t('userList.userRemoved'));
        loadAllUsers();
      } else {
        showToast(result.error || '', 'error');
      }
    } catch (e) {
      console.error('Failed to remove user:', e);
    }
  };

  const defaultUserConfig = (): UserConfig => ({
    display_name: '',
    is_admin: false,
    bound_at: '',
    enabled: true,
    show_message_types: ['assistant'],
    custom_cwd: '',
    routing: {
      agent_name: null,
      model: null,
      reasoning_effort: null,
      opencode_agent: null,
      opencode_model: null,
      opencode_reasoning_effort: null,
      claude_agent: null,
      claude_model: null,
      claude_reasoning_effort: null,
      codex_agent: null,
      codex_model: null,
      codex_reasoning_effort: null,
    },
  });

  const availableMessageTypes = (platform: string): string[] =>
    platformSupportsToolcallDelivery(config, platform) ? ['assistant', 'toolcall'] : ['assistant'];

  const totalCount = aggregated.length;

  // Backend label helper
  const backendLabel = (backend: string) => {
    if (backend === 'claude') return 'Claude';
    if (backend === 'codex') return 'Codex';
    return 'OpenCode';
  };

  return (
    <>
      <div className="flex h-full flex-col gap-5">
        {/* Page header */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-col gap-1.5">
            <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
              {t('userList.title')}
            </h1>
            <p className="text-[14px] leading-[1.55] text-muted">{t('userList.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <SearchField
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('userList.filterPlaceholder')}
              className="w-full sm:w-[280px]"
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => setRefreshTrigger((v) => v + 1)}
              title={t('common.refresh', { defaultValue: 'Refresh' })}
              className="px-3"
            >
              <RefreshCw size={14} />
            </Button>
          </div>
        </div>

        {/* Bind code card */}
        <BindCodeCard
          refreshTrigger={refreshTrigger}
          onCodesChanged={() => setRefreshTrigger((v) => v + 1)}
        />

        {/* List header */}
        <div className="flex items-center justify-between px-1 py-2">
          <div className="flex items-center gap-2">
            <span className="text-[15px] font-semibold text-foreground">{t('userList.allUsers')}</span>
            <span className="rounded-full bg-foreground/[0.08] px-1.5 py-0.5 font-mono text-[11px] font-bold text-muted">
              {totalCount}
            </span>
          </div>
          <label className="flex cursor-pointer items-center gap-1.5 text-[12px] text-muted">
            <span>{t('userList.showDisabled')}</span>
            <ToggleSwitch
              enabled={showDisabled}
              onClick={() => setShowDisabled(!showDisabled)}
            />
          </label>
        </div>

        {/* User cards — design.pen asPXu (VR/RoutingConfig) shared with /groups */}
        <div className="flex-1 space-y-3 overflow-y-auto">
          {visibleUsers.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-[13px] text-muted">
              {searchQuery ? t('userList.noUsersForSearch') : t('userList.noUsers')}
            </div>
          ) : (
            visibleUsers.map((u) => {
              const expanded = expandedKey === u.key;
              const userConfig = u.config;
              const defaultAgent = agentByName[defaultAgentName || ''] || null;
              const selectedAgent = agentByName[userConfig.routing?.agent_name || ''] || agentByName[defaultAgentName || ''];
              const effectiveBackend = selectedAgent?.backend || defaultAgent?.backend || 'opencode';
              const effectiveCwd = userConfig.custom_cwd || config.runtime?.default_cwd || '~/work';
              const opencodeOptions = opencodeOptionsByCwd[effectiveCwd];
              const claudeAgents = claudeAgentsByCwd[effectiveCwd] || [];
              const codexAgents = codexAgentsByCwd[effectiveCwd] || [];
              const isBot = !userConfig.is_admin && (userConfig.display_name || u.userId).toLowerCase().includes('bot');
              const tone = AVATAR_TONES[hashCode(u.userId) % AVATAR_TONES.length];
              const initials = getInitials(userConfig.display_name, u.userId);
              const backendModel = userConfig.routing.model || (
                effectiveBackend === 'claude'
                  ? userConfig.routing.claude_model
                  : effectiveBackend === 'codex'
                    ? userConfig.routing.codex_model
                    : userConfig.routing.opencode_model
              );
              const displayName = displayNameForUser(u);
              const metaPrefix = userConfig.enabled
                ? selectedAgent
                  ? `${selectedAgent.name}${selectedAgent.model ? `/${selectedAgent.model}` : ''}`
                  : `${backendLabel(effectiveBackend)}${backendModel ? `/${backendModel}` : ''}`
                : t('userList.disabled', { defaultValue: 'Disabled' });

              const updateRow = (patch: Partial<UserConfig>) => updateUser(u.platform, u.userId, patch);
              const toggleEnabled = () => updateRow({ enabled: !userConfig.enabled });
              const toggleAdmin = () => handleToggleAdmin(u.platform, u.userId, !userConfig.is_admin);

              return (
                <div
                  key={u.key}
                  className={clsx(
                    'rounded-xl border transition-colors',
                    expanded
                      ? 'border-mint/30 bg-surface-2/70 shadow-[0_0_32px_-8px_rgba(91,255,160,0.45)]'
                      : userConfig.enabled
                        ? 'border-border bg-background hover:border-border-strong'
                        : 'border-border bg-background/60 opacity-70'
                  )}
                >
                  {/* Master row — matches /groups layout */}
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedKey(expanded ? null : u.key)}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === ' ' || e.key === 'Enter') {
                        e.preventDefault();
                        setExpandedKey(expanded ? null : u.key);
                      }
                    }}
                    className="flex w-full cursor-pointer items-center gap-3.5 px-5 py-3.5 text-left"
                  >
                    {/* Enabled toggle */}
                    <span onClick={(e) => e.stopPropagation()}>
                      <ToggleSwitch
                        enabled={userConfig.enabled}
                        onClick={toggleEnabled}
                      />
                    </span>

                    {/* Avatar — tone-colored ring with platform corner badge */}
                    <span className="relative shrink-0">
                      <span
                        className="flex size-[40px] items-center justify-center rounded-full border font-mono text-[13px] font-bold"
                        style={{ backgroundColor: tone.bg, borderColor: tone.border }}
                      >
                        {isBot ? (
                          <Bot size={17} className="text-muted" />
                        ) : (
                          <span className={tone.textCls}>{initials}</span>
                        )}
                      </span>
                      <span
                        className="absolute -bottom-1 -right-1 flex size-[20px] items-center justify-center rounded-full border border-border bg-background shadow-[0_0_0_2px_var(--color-background)]"
                        title={t(`platform.${u.platform}.title`)}
                      >
                        <PlatformIcon platform={u.platform} size={13} />
                      </span>
                    </span>

                    {/* Name + meta */}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-semibold text-foreground">
                        {displayName}
                      </span>
                      <span className="block truncate font-mono text-[11px] text-muted">
                        {metaPrefix} · {u.userId}
                      </span>
                    </span>

                    {/* Admin badge — clickable; bot renders as static label */}
                    {isBot ? (
                      <span className="inline-flex shrink-0 items-center rounded-full border border-border bg-foreground/[0.04] px-2 py-0.5 text-[11px] font-semibold text-muted">
                        {t('userList.bot')}
                      </span>
                    ) : (
                      <span
                        role="button"
                        tabIndex={0}
                        aria-pressed={userConfig.is_admin}
                        onClick={(e) => { e.stopPropagation(); toggleAdmin(); }}
                        onKeyDown={(e) => {
                          if (e.key === ' ' || e.key === 'Enter') {
                            e.preventDefault();
                            e.stopPropagation();
                            toggleAdmin();
                          }
                        }}
                        title={userConfig.is_admin
                          ? t('userList.demoteAdminTitle', { defaultValue: 'Demote admin' })
                          : t('userList.promoteAdminTitle', { defaultValue: 'Promote to admin' })}
                        className={clsx(
                          'inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors',
                          userConfig.is_admin
                            ? 'border-gold/40 bg-gold/10 font-bold text-gold hover:bg-gold/15'
                            : 'border-border bg-foreground/[0.04] text-muted hover:border-border-strong hover:text-foreground'
                        )}
                      >
                        <Shield size={11} strokeWidth={2.4} />
                        {userConfig.is_admin ? t('userList.admin') : t('userList.user')}
                      </span>
                    )}

                    {/* Chevron */}
                    {expanded ? (
                      <ChevronUp size={18} className="shrink-0 text-muted" />
                    ) : (
                      <ChevronDown size={18} className="shrink-0 text-muted" />
                    )}
                  </div>

                  {/* Expanded body — design.pen asPXu (VR/RoutingConfig) shared with /groups, no @mention toggle */}
                  {expanded && (
                    <RoutingConfigPanel
                      value={userConfig}
                      onChange={(patch) => updateRow(patch)}
                      onBrowseDirectory={() => setBrowsingCwdFor(u.key)}
                      globalConfig={config}
                      vibeAgents={vibeAgents}
                      defaultAgentName={defaultAgentName}
                      availableMessageTypes={availableMessageTypes(u.platform)}
                      showRequireMention={false}
                      opencodeOptions={opencodeOptions}
                      claudeAgents={claudeAgents}
                      claudeModels={claudeModels}
                      claudeModelLabels={claudeModelLabels}
                      claudeReasoningOptions={claudeReasoningOptions}
                      codexAgents={codexAgents}
                      codexModels={codexModels}
                      footerActions={
                        <Button
                          type="button"
                          variant="secondary"
                          size="xs"
                          onClick={() => handleRemoveUser(u.platform, u.userId)}
                          title={t('userList.removeUser')}
                          className="text-muted hover:border-danger/40 hover:text-danger"
                        >
                          <Trash2 size={12} />
                          {t('userList.removeUser')}
                        </Button>
                      }
                    />
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Directory browser modal */}
      {browsingCwdFor && (() => {
        const found = aggregated.find((u) => u.key === browsingCwdFor);
        if (!found) return null;
        return (
          <DirectoryBrowser
            initialPath={found.config.custom_cwd || config.runtime?.default_cwd || '~/work'}
            onSelect={(path) => {
              updateUser(found.platform, found.userId, { custom_cwd: path });
              setBrowsingCwdFor(null);
            }}
            onClose={() => setBrowsingCwdFor(null)}
          />
        );
      })()}
    </>
  );
};
