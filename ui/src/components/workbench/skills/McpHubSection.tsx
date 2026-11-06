import { GlobalScopeChip } from './GlobalScopeChip';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Eraser, Globe, Info, KeyRound, Loader2, Plus, RefreshCw, Search, ShieldCheck, Terminal, Trash2, X } from 'lucide-react';
import clsx from 'clsx';
import { useSearchParams } from 'react-router-dom';

import { useApi } from '../../../context/ApiContext';
import type { McpError, McpServerSummary } from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { useInstanceAuthorization } from '../../../context/InstanceAuthorizationContext';
import { isDesktopShell } from '../../../lib/desktopShell';
import { mcpAddOauthAction } from '../../../lib/mcpAddOauth';
import { EMPTY_MCP_ADD_FORM, mcpAddPayload, mcpConfigureMap, mcpConfigurePayload, type McpAddFormState, type McpAddTab, type McpConfigureMap } from '../../../lib/mcpAddForm';
import { mcpPath } from '../../../lib/mcpPath';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from '../../ui/dialog';
import { SegmentedRadio } from '../../ui/segmented';
import { Textarea } from '../../ui/textarea';
import { McpAddForm, McpKeyValueRows } from './McpAddForm';
import { SectionLabel } from './sectionLabel';
import { buttonVariants } from '../../ui/button-variants';

// A project view's login starts, and (via the callback's return URL) ends, in that project.
function oauthStartHref(name: string, project?: string | null): string {
  return `${window.location.origin}${mcpPath(`/${encodeURIComponent(name)}/oauth/start`, project)}`;
}

function LoginLink({
  name,
  project,
  className,
  children,
  onActivate,
}: {
  name: string;
  project?: string | null;
  className?: string;
  children: ReactNode;
  onActivate: (name: string) => void;
}) {
  return (
    <a
      href={oauthStartHref(name, project)}
      target="_blank"
      rel="noopener"
      className={className}
      onClick={() => onActivate(name)}
    >
      {children}
    </a>
  );
}

const SOURCE_CHIP: Record<string, string> = {
  avibe: 'border-gold/40 bg-gold/[0.12] text-gold-ink',
  claude: 'border-mint/40 bg-mint-soft text-mint-ink',
  codex: 'border-violet/40 bg-violet/10 text-violet',
  opencode: 'border-cyan/40 bg-cyan/10 text-cyan-ink',
  project: 'border-border-strong bg-surface-3 text-foreground',
};

const STATE_BADGE: Record<string, 'success' | 'warning' | 'info' | 'destructive'> = {
  ok: 'success',
  needs_login: 'info',
  pending_approval: 'warning',
  needs_config: 'warning',
};

/** The same section header the Skills project view uses (icon · LABEL · hint). */
/** Show actionable state for both scopes; healthy global rows keep their existing look. */
function McpStateBadge({ server }: { server: McpServerSummary }) {
  const { t } = useTranslation();
  if (!server.state || (server.source !== 'project' && server.state === 'ok')) return null;
  const missing = server.state === 'needs_config' ? server.missingVariables ?? [] : [];
  return (
    <Badge variant={STATE_BADGE[server.state] ?? 'destructive'} className="text-[10px]">
      {missing.length
        ? t('mcpHub.stateMissing', { state: t('mcpHub.state.needs_config'), names: missing.join(', ') })
        : t(`mcpHub.state.${server.state}`, { defaultValue: server.state })}
    </Badge>
  );
}

function McpToolCard({
  name,
  description,
  expanded,
  onToggle,
}: {
  name: string;
  description?: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const textRef = useRef<HTMLParagraphElement>(null);
  const [truncated, setTruncated] = useState(false);

  useLayoutEffect(() => {
    const node = textRef.current;
    if (!node || expanded) return;
    setTruncated(node.scrollHeight > node.clientHeight + 1);
  }, [description, expanded]);

  if (!description) {
    return (
      <div className="rounded-[10px] border border-border bg-surface-3 px-3 py-2">
        <div className="font-mono text-[12px] text-foreground">{name}</div>
      </div>
    );
  }

  const interactive = truncated || expanded;
  return (
    <button
      type="button"
      disabled={!interactive}
      aria-expanded={interactive ? expanded : undefined}
      onClick={() => interactive && onToggle()}
      className={clsx(
        'w-full rounded-[10px] border border-border bg-surface-3 px-3 py-2 text-left',
        interactive ? 'hover:border-border-strong' : 'cursor-default',
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[12px] text-foreground">{name}</div>
          <p ref={textRef} className={clsx('text-[11px] text-muted', !expanded && 'line-clamp-2')}>
            {description}
          </p>
        </div>
        {interactive ? (
          <ChevronDown className={clsx('mt-0.5 size-3.5 shrink-0 text-muted transition-transform', expanded && 'rotate-180')} />
        ) : null}
      </div>
    </button>
  );
}

export interface McpServerDetailProps {
  server: McpServerSummary;
  /** The Avibe project of the view, kept on its login link. */
  project?: string | null;
  canManage: boolean;
  /** Tool cards the user opened, by tool name. */
  expandedTools: Record<string, boolean>;
  /** Another MCP operation is running; the actions wait for it. */
  busy: boolean;
  refreshing: boolean;
  onToggleTool: (name: string) => void;
  onClose: () => void;
  onRefresh: () => void;
  onRemove: () => void;
  onApprove?: () => void;
  onConfigure?: () => void;
  onActivateLogin: (name: string) => void;
}

/** The detail panel of the selected server (ui/design: skills/mcp-detail/refreshing). */
export function McpServerDetail({
  server: current,
  project,
  canManage,
  expandedTools,
  busy,
  refreshing,
  onToggleTool,
  onClose,
  onRefresh,
  onRemove,
  onApprove,
  onConfigure,
  onActivateLogin,
}: McpServerDetailProps) {
  const { t } = useTranslation();
  const projectRow = Boolean(current.scope?.startsWith('project:'));
  const owned = projectRow || current.source === 'avibe';
  const state = current.state ?? 'ok';
  // A project server has tools (and can be refreshed) once approved and configured.
  const usable = !['pending_approval', 'needs_config'].includes(state);
  return (
    <div className="flex min-w-0 flex-col gap-3.5 self-start rounded-2xl border border-border-strong bg-surface p-5">
      <div className="flex items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-[10px] border border-border bg-surface-3">
          {current.transport === 'stdio' ? <Terminal className="size-5" /> : <Globe className="size-5" />}
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex flex-wrap items-center gap-2">
            <span className="max-w-full truncate font-mono text-[16px] font-bold text-foreground">{current.name}</span>
            <Badge className={clsx('font-mono text-[9px] uppercase', SOURCE_CHIP[current.source ?? 'avibe'])}>
              {t(`mcpHub.source.${current.source ?? 'avibe'}`)}
            </Badge>
            <McpStateBadge server={current} />
          </div>
          <div className="truncate font-mono text-[11px] text-muted" title={current.command || current.url || undefined}>
            {current.command || current.url}
          </div>
          {projectRow && current.sourcePath ? (
            <div className="truncate font-mono text-[10px] text-muted" title={current.sourcePath}>{current.sourcePath}</div>
          ) : null}
        </div>
        <Button type="button" variant="ghost" size="icon" className="size-6" onClick={onClose} aria-label={t('common.close')}>
          <X className="size-3.5" />
        </Button>
      </div>
      {owned && (state === 'pending_approval' || state === 'needs_config') ? (
        <div className="rounded-[10px] border border-gold/40 bg-gold/10 px-3 py-2.5 text-[12px] text-foreground">
          {state === 'pending_approval'
            ? t('mcpHub.pendingApprovalHint')
            : t('mcpHub.needsConfigHint', { names: (current.missingVariables ?? []).join(', ') })}
        </div>
      ) : null}
      {owned && state === 'not_generated' ? (
        <div className="rounded-[10px] border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-[12px] text-foreground">
          {t('mcpHub.notGeneratedHint')}
        </div>
      ) : null}
      {usable ? (
      <>
      <div className="flex flex-col gap-1.5">
        <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('mcpHub.tools')}</span>
        {(current.tools ?? []).map((tool) => (
          <McpToolCard
            key={tool.name}
            name={tool.name}
            description={tool.description}
            expanded={Boolean(expandedTools[tool.name])}
            onToggle={() => onToggleTool(tool.name)}
          />
        ))}
      </div>
      <div className="rounded-[10px] border border-border bg-surface-3 px-3 py-2.5 font-mono text-[11px] text-muted">
        {t('mcpHub.callHint', { name: current.name })}
      </div>
      <div className="font-mono text-[10px] text-muted">
        {current.generatedAt && current.serverVersion
          ? t('mcpHub.generatedAtWithVersion', { time: current.generatedAt, version: current.serverVersion })
          : current.generatedAt
            ? t('mcpHub.generatedAt', { time: current.generatedAt })
            : current.serverVersion
              ? t('mcpHub.serverVersion', { version: current.serverVersion })
              : null}
      </div>
      </>
      ) : null}
      {!(project && current.source !== 'project') && current.auth === 'needs_login' ? <LoginLink name={current.name} project={project} className={buttonVariants({ variant: 'brand', size: 'sm' })} onActivate={onActivateLogin}>{t('mcpHub.login')}</LoginLink> : null}
      {canManage ? (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {projectRow && state === 'pending_approval' && onApprove ? (
            <Button type="button" variant="brand" size="xs" disabled={busy} onClick={onApprove}>
              <ShieldCheck className="size-3.5" />
              {t('mcpHub.approve')}
            </Button>
          ) : null}
          {owned && onConfigure ? (
            <Button type="button" variant={state === 'needs_config' ? 'brand' : 'outline'} size="xs" disabled={busy} onClick={onConfigure}>
              <KeyRound className="size-3.5" />
              {t('mcpHub.configure')}
            </Button>
          ) : null}
          {usable ? (
          <Button type="button" variant="outline" size="xs" disabled={busy} onClick={onRefresh}>
            {refreshing ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            {t('mcpHub.refresh')}
          </Button>
          ) : null}
          <Button type="button" variant="ghost" size="xs" onClick={onRemove}>
            <Trash2 className="size-3.5" />
            {t('mcpHub.remove')}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export interface McpLoginDialogProps {
  /** The server being logged in to in paste mode; null keeps the dialog closed. */
  name: string | null;
  project?: string | null;
  callbackText: string;
  busy: boolean;
  onCallbackTextChange: (text: string) => void;
  onComplete: () => void;
  onClose: () => void;
  onActivateLogin: (name: string) => void;
}

/** Paste-mode OAuth: authorize in a new tab, then paste the callback URL (ui/design: skills/mcp/login-paste). */
export function McpLoginDialog({ name, project, callbackText, busy, onCallbackTextChange, onComplete, onClose, onActivateLogin }: McpLoginDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={name !== null} onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
        <DialogTitle>{t('mcpHub.loginTitle')}</DialogTitle>
        <DialogDescription>{t('mcpHub.loginHint')}</DialogDescription>
        {name ? <LoginLink name={name} project={project} className={buttonVariants({ variant: 'outline' })} onActivate={onActivateLogin}>{t('mcpHub.openAuthorize')}</LoginLink> : null}
        <Textarea value={callbackText} onChange={(event) => onCallbackTextChange(event.target.value)} className="min-h-20 font-mono text-[12px]" placeholder={t('mcpHub.callbackPlaceholder')} />
        <DialogFooter><Button type="button" variant="outline" onClick={onClose}>{t('common.cancel')}</Button><Button type="button" disabled={!callbackText.trim() || busy} onClick={onComplete}>{t('mcpHub.completeLogin')}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface McpAddDialogProps {
  open: boolean;
  /** The Avibe project the servers are added to (its `.mcp.json`, pending approval); null adds to the global hub. */
  project?: { id: string; name: string } | null;
  text: string;
  error: string | null;
  /** After an add whose servers need login: their start links, and the lines that failed. */
  result: { names: string[]; failedLines: string[] } | null;
  /** Another MCP operation is running; Add waits for it. */
  busy: boolean;
  adding: boolean;
  onTextChange: (text: string) => void;
  /** Paste JSON (default) or the form; each keeps its input across switches. */
  tab: McpAddTab;
  onTabChange: (tab: McpAddTab) => void;
  form: McpAddFormState;
  onFormChange: (form: McpAddFormState) => void;
  onAdd: () => void;
  onClose: () => void;
  onDone: () => void;
  onActivateLogin: (name: string) => void;
}

/** Paste `mcpServers` JSON or fill a form to add servers (ui/design: skills/mcp/add, skills/mcp/add-form*). */
export function McpAddDialog({ open, project, text, error, result, busy, adding, onTextChange, tab, onTabChange, form, onFormChange, onAdd, onClose, onDone, onActivateLogin }: McpAddDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onClose() : undefined)}>
      <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
        <DialogTitle>{t('mcpHub.addTitle')}</DialogTitle>
        <DialogDescription>
          {result ? t('mcpHub.addResultHint') : project ? t('mcpHub.addProjectSubtitle', { project: project.name }) : t('mcpHub.addSubtitle')}
        </DialogDescription>
        {result ? (
          <div className="flex flex-col gap-2">
            {result.names.map((name) => (
              <p key={name} className="flex items-center gap-2 text-[12px] text-foreground">
                <span>{t('mcpHub.needsLoginAfterAdd', { name })}</span>
                <LoginLink name={name} project={project?.id} className={buttonVariants({ variant: 'brand', size: 'xs' })} onActivate={onActivateLogin}>
                  {t('mcpHub.goAuthorize')}
                </LoginLink>
              </p>
            ))}
            {result.failedLines.map((line) => (
              <div key={line} className="text-[12px] text-destructive-ink">{line}</div>
            ))}
          </div>
        ) : (
          <>
            <SegmentedRadio
              value={tab}
              onChange={onTabChange}
              ariaLabel={t('mcpHub.addMode')}
              options={[
                { id: 'json', label: t('mcpHub.addModeJson') },
                { id: 'form', label: t('mcpHub.addModeForm') },
              ]}
            />
            {tab === 'json' ? (
              <>
                <Textarea value={text} onChange={(event) => onTextChange(event.target.value)} className="min-h-40 font-mono text-[12px]" placeholder={t('mcpHub.pastePlaceholder')} />
                <p className="text-[11px] text-muted">{t('mcpHub.pasteHint')}</p>
              </>
            ) : (
              <McpAddForm value={form} onChange={onFormChange} />
            )}
            {error ? <div className="whitespace-pre-wrap text-[12px] text-destructive-ink">{error}</div> : null}
          </>
        )}
        <DialogFooter>
          {result ? (
            <Button type="button" variant="brand" onClick={onDone}>{t('common.done')}</Button>
          ) : (
            <>
              <Button type="button" variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
              <Button type="button" disabled={busy || mcpAddPayload(tab, text, form) === null} onClick={onAdd}>
                {adding ? <Loader2 className="size-3.5 animate-spin" /> : null}
                {t('mcpHub.add')}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface McpRemoveDialogProps {
  /** The server to delete; null keeps the dialog closed. */
  server: McpServerSummary | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirms deleting a server, from Avibe or from its native config file (ui/design: skills/mcp/remove). */
export function McpRemoveDialog({ server, busy, onConfirm, onCancel }: McpRemoveDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={server !== null} onOpenChange={(open) => (!open ? onCancel() : undefined)}>
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogTitle>{t('mcpHub.removeConfirm', { name: server?.name ?? '' })}</DialogTitle>
        <DialogDescription>
          {server?.source === 'avibe' ? t('mcpHub.removeAvibeHint') : server?.scope?.startsWith('project:') ? t('mcpHub.removeProjectHint', {
                name: server.name,
                path: server.sourcePath ?? '',
              }) : t('mcpHub.removeNativeHint', {
                name: server?.name ?? '',
                path: server?.sourcePath ?? '',
                agent: t(`mcpHub.source.${server?.source ?? 'avibe'}`),
              })}
        </DialogDescription>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
            {t('mcpHub.remove')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface McpApproveDialogProps {
  /** The project server to approve; null keeps the dialog closed. */
  server: McpServerSummary | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirms trusting a repository-defined command or URL (ui/design: skills/mcp/approve). */
export function McpApproveDialog({ server, busy, onConfirm, onCancel }: McpApproveDialogProps) {
  const { t } = useTranslation();
  const stdio = server?.transport === 'stdio';
  return (
    <Dialog open={server !== null} onOpenChange={(open) => (!open ? onCancel() : undefined)}>
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogTitle>{t('mcpHub.approveConfirm', { name: server?.name ?? '' })}</DialogTitle>
        <DialogDescription>
          {t(stdio ? 'mcpHub.approveCommandHint' : 'mcpHub.approveUrlHint', { path: server?.sourcePath ?? '' })}
        </DialogDescription>
        <div className="break-all rounded-[10px] border border-border bg-surface-3 px-3 py-2 font-mono text-[12px] text-foreground">
          {server?.command || server?.url}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button type="button" variant="brand" disabled={busy} onClick={onConfirm}>
            {t('mcpHub.approve')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface McpConfigureDialogProps {
  /** The Avibe-owned server to configure; null keeps the dialog closed. */
  server: McpServerSummary | null;
  env: McpConfigureMap;
  headers: McpConfigureMap;
  error: string | null;
  busy: boolean;
  onEnvChange: (map: McpConfigureMap) => void;
  onHeadersChange: (map: McpConfigureMap) => void;
  onSave: () => void;
  onCancel: () => void;
}

/** Sets an Avibe-owned server's `${VAR}` values and headers; stored values are never shown (ui/design: skills/mcp/configure). */
export function McpConfigureDialog({ server, env, headers, error, busy, onEnvChange, onHeadersChange, onSave, onCancel }: McpConfigureDialogProps) {
  const { t } = useTranslation();
  const missing = server?.missingVariables ?? [];
  // Editing rows marks the map for replacement; Clear is the one explicit way to send an empty map.
  const section = (label: string, map: McpConfigureMap, onChange: (map: McpConfigureMap) => void, keyPlaceholder: string) => (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-1.5 text-[12px] text-foreground">
        <span className="flex-1">{label}</span>
        {map.cleared ? (
          <Button type="button" variant="outline" size="xs" onClick={() => onChange({ ...map, cleared: false })}>
            {t('mcpHub.configureUndoClear')}
          </Button>
        ) : (
          <>
            <Button type="button" variant="ghost" size="xs" onClick={() => onChange({ ...map, cleared: true })}>
              <Eraser className="size-3" />
              {t('mcpHub.configureClear')}
            </Button>
            <Button type="button" variant="outline" size="xs" onClick={() => onChange({ ...map, edited: true, rows: [...map.rows, { key: '', value: '' }] })}>
              <Plus className="size-3" />
              {t('mcpHub.form.addRow')}
            </Button>
          </>
        )}
      </div>
      {map.cleared ? (
        <p className="text-[12px] text-muted">{t('mcpHub.configureCleared')}</p>
      ) : (
        <McpKeyValueRows
          label={label}
          rows={map.rows}
          onChange={(rows) => onChange({ ...map, edited: true, rows })}
          keyPlaceholder={keyPlaceholder}
          secret
          allowEmpty
        />
      )}
    </div>
  );
  return (
    <Dialog open={server !== null} onOpenChange={(open) => (!open ? onCancel() : undefined)}>
      <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
        <DialogTitle>{t('mcpHub.configureTitle', { name: server?.name ?? '' })}</DialogTitle>
        <DialogDescription>{t('mcpHub.configureHint')}</DialogDescription>
        {missing.length ? (
          <p className="text-[12px] text-foreground">{t('mcpHub.configureMissing', { names: missing.join(', ') })}</p>
        ) : null}
        {section(t('mcpHub.configureVariables'), env, onEnvChange, t('mcpHub.form.envKeyPlaceholder'))}
        {server?.transport !== 'stdio'
          ? section(t('mcpHub.form.headers'), headers, onHeadersChange, t('mcpHub.form.headerKeyPlaceholder'))
          : null}
        {error ? <div className="whitespace-pre-wrap text-[12px] text-destructive-ink">{error}</div> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button type="button" variant="brand" disabled={busy || mcpConfigurePayload(env, headers) === null} onClick={onSave}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {t('mcpHub.configureSave')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface McpHubSectionProps {
  /** The parent toolbar owns search when the hub is embedded in SkillsPage. */
  searchQuery?: string;
  /** The Avibe project whose view this is (its rows plus the visible global ones); null is the global hub. */
  project?: { id: string; name: string } | null;
}

interface McpRowProps {
  server: McpServerSummary;
  /** Dim + tag a row inherited from global into a project view (view-only there). */
  inherited?: boolean;
  selected?: boolean;
  /** Show the OAuth login link in the row. */
  loginLink?: boolean;
  onSelect: () => void;
  onActivateLogin: (name: string) => void;
  projectId?: string | null;
}

/** One MCP server row, in the same shape as the Skills rows (SkillRow + its inherited look). */
function McpRow({ server, inherited, selected, loginLink, onSelect, onActivateLogin, projectId }: McpRowProps) {
  const { t } = useTranslation();
  const transportLabel = server.transport === 'stdio' ? t('mcpHub.transportStdio') : t('mcpHub.transportHttp');
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        'group flex w-full items-center gap-3 rounded-lg border px-[14px] py-3 text-left transition',
        selected
          ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
        inherited && 'opacity-[0.66]',
      )}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-[9px] border border-border bg-surface-3 text-muted">
        {server.transport === 'stdio' ? <Terminal className="size-4" /> : <Globe className="size-4" />}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-[13px] font-semibold text-foreground">{server.name}</span>
          {inherited ? (
            <GlobalScopeChip />
          ) : (
            <Badge className={clsx('font-mono text-[9px] uppercase', SOURCE_CHIP[server.source ?? 'avibe'])}>
              {t(`mcpHub.source.${server.source ?? 'avibe'}`)}
            </Badge>
          )}
          <McpStateBadge server={server} />
        </span>
        <span className="truncate font-mono text-[11px] text-muted" title={server.command || server.url || server.description || undefined}>
          {server.command || server.url || server.description}
        </span>
        <span className="font-mono text-[10px] text-muted">
          {t('mcpHub.toolMeta', { count: server.toolCount ?? 0, transport: transportLabel })}
        </span>
      </span>
      {!inherited && loginLink && server.auth === 'needs_login' ? (
        <span onClick={(event) => event.stopPropagation()}>
          <LoginLink
            name={server.name}
            project={projectId}
            className="inline-flex h-6 items-center rounded-md border border-border bg-surface px-2 font-mono text-[10px] font-semibold uppercase text-foreground hover:bg-surface-2"
            onActivate={onActivateLogin}
          >
            {t('mcpHub.login')}
          </LoginLink>
        </span>
      ) : null}
      <ChevronRight className="size-3.5 text-muted" />
    </button>
  );
}

export const McpHubSection: React.FC<McpHubSectionProps> = ({ project = null, searchQuery }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canManage = capabilities.is_instance_owner;
  const projectId = project?.id ?? null;
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [oauthMode, setOauthMode] = useState<'direct' | 'paste'>('direct');
  const [addResult, setAddResult] = useState<{ names: string[]; failedLines: string[] } | null>(null);
  const [search, setSearch] = useState('');
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedServer = searchParams.get('mcp');
  const [selected, setSelected] = useState<string | null>(linkedServer);
  // A `?mcp=` link that changes while the hub is open selects its server; clicks since stay the user's.
  const [arrivedServer, setArrivedServer] = useState(linkedServer);
  if (linkedServer !== arrivedServer) {
    setArrivedServer(linkedServer);
    if (linkedServer) setSelected(linkedServer);
  }
  const [expandedFor, setExpandedFor] = useState<string | null>(null);
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [addText, setAddText] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [addTab, setAddTab] = useState<McpAddTab>('json');
  const [addForm, setAddForm] = useState<McpAddFormState>(EMPTY_MCP_ADD_FORM);
  const [loginFor, setLoginFor] = useState<{ name: string } | null>(null);
  const [callbackText, setCallbackText] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<McpServerSummary | null>(null);
  const [confirmApprove, setConfirmApprove] = useState<McpServerSummary | null>(null);
  const [configureFor, setConfigureFor] = useState<McpServerSummary | null>(null);
  const [configureEnv, setConfigureEnv] = useState<McpConfigureMap>(mcpConfigureMap);
  const [configureHeaders, setConfigureHeaders] = useState<McpConfigureMap>(mcpConfigureMap);
  const [configureError, setConfigureError] = useState<string | null>(null);
  const loginInFlight = useRef(false);

  if (expandedFor !== selected) {
    setExpandedFor(selected);
    setExpandedTools({});
  }

  const describe = useCallback(
    (error?: McpError | string) => {
      if (typeof error === 'string' && error.trim()) return error;
      if (error && typeof error === 'object') {
        const type = typeof error.type === 'string' ? error.type.trim() : '';
        const message = typeof error.message === 'string' ? error.message : '';
        if (type) return t(`mcpHub.errors.${type}`, { defaultValue: message || t('mcpHub.failed'), message });
        if (message) return message;
      }
      return t('mcpHub.failed');
    },
    [t],
  );

  const load = useCallback(async () => {
    try {
      const res = await api.listMcpServers(projectId);
      if (res.ok) {
        setServers(res.servers ?? []);
        if (res.oauthMode === 'direct' || res.oauthMode === 'paste') setOauthMode(res.oauthMode);
      }
      else showToast(describe(res.error), 'error');
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    }
  }, [api, describe, projectId, showToast, t]);

  // The OAuth login returns with `?oauth=success`: say so once, and drop it so a reload does not.
  const oauthReturn = searchParams.get('oauth') === 'success';
  const oauthAnnounced = useRef(false);
  useEffect(() => {
    if (!oauthReturn || oauthAnnounced.current) return;
    oauthAnnounced.current = true;
    showToast(t('mcpHub.loginComplete'), 'success');
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete('oauth');
      return next;
    }, { replace: true });
  }, [oauthReturn, setSearchParams, showToast, t]);
  const [listed, setListed] = useState(false);
  if (!listed) {
    setListed(true);
    void load();
  }

  const filtered = useMemo(() => {
    const q = (searchQuery ?? search).trim().toLowerCase();
    if (!q) return servers;
    return servers.filter((item) => item.name.toLowerCase().includes(q) || (item.description || '').toLowerCase().includes(q));
  }, [searchQuery, search, servers]);
  // Project view grouping mirrors Skills: this project's `.mcp.json` rows first,
  // then the inherited global rows (the backend already drops globals shadowed
  // by a same-named project server).
  const projectRows = useMemo(() => filtered.filter((server) => projectId && server.scope?.startsWith('project:')), [filtered, projectId]);
  const inheritedRows = useMemo(() => filtered.filter((server) => !projectId || !server.scope?.startsWith('project:')), [filtered, projectId]);

  const current = servers.find((item) => item.name === selected) ?? null;
  const currentInherited = Boolean(projectId && current && !current.scope?.startsWith('project:'));
  const renderRows = (rows: McpServerSummary[], inherited = false) => (
    <div className="flex flex-col gap-2">{rows.map((server) => (
      <McpRow
        key={server.name}
        server={server}
        inherited={inherited}
        selected={selected === server.name}
        onSelect={() => setSelected(server.name)}
        loginLink={server.auth === 'needs_login'}
        onActivateLogin={onActivateLogin}
        projectId={projectId}
      />
    ))}</div>
  );

  const onDiscover = async () => {
    if (!canManage) return;
    setBusy('discover');
    try {
      const res = await api.discoverMcp(projectId);
      if (!res.ok) showToast(describe(res.error), 'error');
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onRefreshOne = async (name: string) => {
    setBusy(`refresh:${name}`);
    try {
      const res = await api.refreshMcp(name, projectId);
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      showToast(t('mcpHub.refreshed', { name, count: res.toolCount ?? 0 }), 'success');
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onActivateLogin = (name: string) => {
    loginInFlight.current = true;
    if (oauthMode === 'paste') setLoginFor({ name });
  };

  useEffect(() => {
    const refreshIfWaiting = () => {
      if (!loginInFlight.current || document.visibilityState !== 'visible') return;
      loginInFlight.current = false;
      void load();
    };
    window.addEventListener('focus', refreshIfWaiting);
    document.addEventListener('visibilitychange', refreshIfWaiting);
    return () => {
      window.removeEventListener('focus', refreshIfWaiting);
      document.removeEventListener('visibilitychange', refreshIfWaiting);
    };
  }, [load]);

  const onCompleteLogin = async () => {
    if (!loginFor) return;
    setBusy(`login:${loginFor.name}`);
    try {
      const res = await api.completeMcpLogin(loginFor.name, callbackText, projectId);
      if (!res.ok) { showToast(describe(res.error), 'error'); return; }
      setLoginFor(null); setCallbackText(''); await load();
      showToast(t('mcpHub.loginComplete'), 'success');
    } finally { setBusy(null); }
  };
  const closeAdd = () => {
    setShowAdd(false);
    setAddText('');
    setAddTab('json');
    setAddForm(EMPTY_MCP_ADD_FORM);
    setAddError(null);
    setAddResult(null);
  };

  const addPayload = mcpAddPayload(addTab, addText, addForm);

  const onAdd = async () => {
    if (addPayload === null) return;
    setBusy('add');
    setAddError(null);
    setAddResult(null);
    try {
      const res = await api.addMcp(addPayload, projectId);
      const added = res.added ?? [];
      const unchanged = res.unchanged ?? [];
      const failed = res.failed ?? [];
      const succeeded = added.length + unchanged.length;
      const failedLines = failed.map((item) =>
        t('mcpHub.failedLine', {
          name: item.name,
          error: item.type ? t(`mcpHub.errors.${item.type}`, { defaultValue: item.error || '', message: item.error || '', name: item.name }) : (item.error || ''),
        }),
      );
      const inline = [
        t('mcpHub.succeededCount', { count: succeeded }),
        failed.length ? t('mcpHub.failedCount', { count: failed.length }) : null,
        ...failedLines,
      ].filter(Boolean).join('\n');
      const hasBatch = Boolean(res.added || res.unchanged || res.failed);
      if (!succeeded) {
        setAddError(hasBatch ? inline : describe(res.error));
        return;
      }
      const names = [...added, ...unchanged]
        .filter((item) => item.auth === 'needs_login')
        .map((item) => item.name);
      const action = mcpAddOauthAction({
        names,
        failedCount: failed.length,
        oauthMode,
        desktopShell: isDesktopShell(),
      });
      if (action.kind === 'assign') {
        closeAdd();
        window.location.assign(oauthStartHref(action.name, projectId));
        return;
      }
      if (failed.length) {
        showToast(inline, 'error', { label: t('common.close'), onClick: () => undefined });
      } else {
        showToast(t('mcpHub.succeededCount', { count: succeeded }), 'success');
      }
      if (action.kind === 'list') {
        setAddResult({ names: action.names, failedLines });
        await load();
        return;
      }
      closeAdd();
      await load();
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    } finally {
      setBusy(null);
    }
  };

  const onDelete = async () => {
    if (!confirmDelete) return;
    const name = confirmDelete.name;
    setBusy(`remove:${name}`);
    try {
      const res = await api.removeMcp(name, projectId);
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      setConfirmDelete(null);
      if (selected === name) setSelected(null);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const onApprove = async () => {
    if (!confirmApprove || !projectId) return;
    const name = confirmApprove.name;
    setBusy(`approve:${name}`);
    try {
      const res = await api.approveMcp(name, projectId);
      if (!res.ok) {
        showToast(describe(res.error), 'error');
        return;
      }
      setConfirmApprove(null);
      await load();
    } catch {
      showToast(t('mcpHub.failed'), 'error');
    } finally {
      setBusy(null);
    }
  };

  // Stored values are never shown: the editor starts with each `${VAR}` name and an empty value.
  const openConfigure = (server: McpServerSummary) => {
    setConfigureFor(server);
    setConfigureEnv(mcpConfigureMap((server.placeholders?.length ? server.placeholders : server.missingVariables ?? []).map((key) => ({ key, value: '' }))));
    setConfigureHeaders(mcpConfigureMap());
    setConfigureError(null);
  };

  const onConfigure = async () => {
    const secrets = mcpConfigurePayload(configureEnv, configureHeaders);
    if (!configureFor || secrets === null) return;
    const name = configureFor.name;
    setBusy(`configure:${name}`);
    setConfigureError(null);
    try {
      const res = await api.configureMcp(name, secrets, projectId);
      if (!res.ok) {
        setConfigureError(describe(res.error));
        await load();
        return;
      }
      setConfigureFor(null);
      setConfigureEnv(mcpConfigureMap());
      setConfigureHeaders(mcpConfigureMap());
      await load();
    } catch {
      setConfigureError(t('mcpHub.failed'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2.5">
        {searchQuery === undefined ? (<div className="flex min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border-strong bg-surface px-3 py-2">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t(projectId ? 'mcpHub.searchProjectPlaceholder' : 'mcpHub.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>
        ) : null}
        {canManage ? (
          <>
            <Button type="button" variant="outline" size="xs" disabled={busy !== null} onClick={() => void onDiscover()}>
              {busy === 'discover' ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {t('mcpHub.refreshAll')}
            </Button>
            <Button type="button" variant="brand" size="xs" onClick={() => { setShowAdd(true); setAddError(null); setAddResult(null); }}>
              <Plus className="size-3.5" />
              {t('mcpHub.add')}
            </Button>
          </>
        ) : null}
      </div>

      <div className={clsx('grid gap-5', 'grid-cols-1', current && 'lg:grid-cols-[minmax(0,1fr)_400px]')}>
        <div className="flex min-w-0 flex-col gap-2">
          {projectId ? (
            <>
              <div className="flex flex-col gap-2">
                <SectionLabel
                  icon={<ShieldCheck className="size-3.5 text-mint-ink" />}
                  label={t('mcpHub.projectSectionLocal')}
                  hint={t('mcpHub.projectSectionLocalHint')}
                />
                {projectRows.length > 0 ? (
                  renderRows(projectRows)
                ) : (
                  <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12px] text-muted">
                    {filtered.length === 0 && servers.length > 0 ? t('mcpHub.noSearchMatch') : t('mcpHub.projectEmpty')}
                  </div>
                )}
              </div>
              {inheritedRows.length > 0 ? (
                <div className="flex flex-col gap-2">
                  <SectionLabel
                    icon={<Info className="size-3.5 text-muted" />}
                    label={t('mcpHub.projectSectionGlobal')}
                    hint={t('mcpHub.projectSectionGlobalHint')}
                  />
                  {renderRows(inheritedRows, true)}
                </div>
              ) : null}
            </>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
              <p className="text-[13px] text-muted">{servers.length === 0 ? t('mcpHub.empty') : t('mcpHub.noSearchMatch')}</p>
              {canManage && servers.length === 0 ? (
                <Button type="button" variant="brand" size="sm" onClick={() => setShowAdd(true)}>
                  <Plus className="size-3.5" />
                  {t('mcpHub.add')}
                </Button>
              ) : null}
            </div>
          ) : (
            renderRows(filtered)
          )}
        </div>

        {current ? (
          <McpServerDetail
            server={current}
            project={projectId}
            canManage={canManage && !currentInherited}
            expandedTools={expandedTools}
            busy={busy !== null}
            refreshing={busy === `refresh:${current.name}`}
            onToggleTool={(name) => setExpandedTools((prev) => ({ ...prev, [name]: !prev[name] }))}
            onClose={() => setSelected(null)}
            onRefresh={() => void onRefreshOne(current.name)}
            onRemove={() => setConfirmDelete(current)}
            onApprove={projectId ? () => setConfirmApprove(current) : undefined}
            onConfigure={current.source === 'avibe' || (current.source === 'project' && current.scope?.startsWith('project:')) ? () => openConfigure(current) : undefined}
            onActivateLogin={onActivateLogin}
          />
        ) : null}
      </div>

      <McpLoginDialog
        name={loginFor?.name ?? null}
        project={projectId}
        callbackText={callbackText}
        busy={busy !== null}
        onCallbackTextChange={setCallbackText}
        onComplete={() => void onCompleteLogin()}
        onClose={() => setLoginFor(null)}
        onActivateLogin={onActivateLogin}
      />

      <McpAddDialog
        open={showAdd}
        project={project}
        text={addText}
        error={addError}
        result={addResult}
        busy={busy !== null}
        adding={busy === 'add'}
        onTextChange={setAddText}
        tab={addTab}
        onTabChange={setAddTab}
        form={addForm}
        onFormChange={setAddForm}
        onAdd={() => void onAdd()}
        onClose={closeAdd}
        onDone={() => { closeAdd(); void load(); }}
        onActivateLogin={onActivateLogin}
      />

      <McpRemoveDialog
        server={confirmDelete}
        busy={busy !== null}
        onConfirm={() => void onDelete()}
        onCancel={() => setConfirmDelete(null)}
      />

      <McpApproveDialog
        server={confirmApprove}
        busy={busy !== null}
        onConfirm={() => void onApprove()}
        onCancel={() => setConfirmApprove(null)}
      />

      <McpConfigureDialog
        server={configureFor}
        env={configureEnv}
        headers={configureHeaders}
        error={configureError}
        busy={busy !== null}
        onEnvChange={setConfigureEnv}
        onHeadersChange={setConfigureHeaders}
        onSave={() => void onConfigure()}
        onCancel={() => { setConfigureFor(null); setConfigureEnv(mcpConfigureMap()); setConfigureHeaders(mcpConfigureMap()); }}
      />
    </div>
  );
};
