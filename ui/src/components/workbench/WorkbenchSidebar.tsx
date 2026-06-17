import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity,
  Archive,
  ArrowRight,
  Bot,
  ChevronDown,
  ChevronRight,
  Ellipsis,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  GitFork,
  Hash,
  Inbox,
  KeyRound,
  Loader2,
  Pencil,
  Plus,
  Settings2,
  WandSparkles,
} from 'lucide-react';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';

import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useComposerInsertTarget } from '../../context/ComposerBridgeContext';
import type { InboxSession, WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from '../ui/popover';
import { ArchiveSessionDialog } from './ArchiveSessionDialog';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Markdown } from '../ui/markdown';
import { NewProjectDialog } from './NewProjectDialog';
import { ProjectAgentsMdDialog } from './ProjectAgentsMdDialog';
import { ProjectSettingsDialog } from './ProjectSettingsDialog';

interface CapabilityNavItem {
  to: string;
  i18nKey: string;
  icon: LucideIcon;
}

const CAPABILITY_NAV: CapabilityNavItem[] = [
  { to: '/agents', i18nKey: 'workbench.nav.agents', icon: Bot },
  { to: '/skills', i18nKey: 'workbench.nav.skills', icon: WandSparkles },
  { to: '/harness', i18nKey: 'workbench.nav.harness', icon: Activity },
  { to: '/vaults', i18nKey: 'workbench.nav.vaults', icon: KeyRound },
];

// 360px floating popover that opens when the user hovers the Inbox entry.
// Mirrors design.pen KmQ1L — header + a few session cards + footer "open full
// inbox" link. Pure presentational; data comes from <WorkbenchInboxProvider>.
const InboxHoverPopover: React.FC<{
  visible: boolean;
  sessions: InboxSession[];
  unreadBySession: Record<string, number>;
  unreadSessions: number;
  totalUnread: number;
  onItemClick: (session: InboxSession) => void;
  onMarkAllRead: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}> = ({
  visible,
  sessions,
  unreadBySession,
  unreadSessions,
  totalUnread,
  onItemClick,
  onMarkAllRead,
  onMouseEnter,
  onMouseLeave,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  if (!visible) return null;
  const shown = sessions.slice(0, 5);
  // The unread map is authoritative; a session absent from it has 0 unread
  // (don't fall back to the card's stale unread_count — see InboxPage).
  const unreadOf = (s: InboxSession) => unreadBySession[s.session_id] ?? 0;
  return (
    <div
      role="dialog"
      aria-label={t('workbench.inbox.title')}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="absolute left-full top-0 z-50 ml-3 flex w-[360px] flex-col gap-2.5 rounded-2xl border border-border-strong bg-surface-2 p-3.5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
    >
      <div className="flex items-start gap-2">
        <div className="flex flex-1 flex-col">
          <div className="text-[13px] font-bold text-foreground">{t('workbench.inbox.title')}</div>
          <div className="text-[10px] text-muted">
            {t('workbench.inbox.headerCount', { unread: unreadSessions, total: sessions.length })}
          </div>
        </div>
        <button
          type="button"
          onClick={onMarkAllRead}
          disabled={totalUnread === 0}
          className={clsx(
            'rounded-md border px-2 py-1 text-[10px] font-medium transition',
            totalUnread === 0
              ? 'cursor-not-allowed border-border bg-foreground/[0.02] text-muted'
              : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
          )}
        >
          {t('workbench.inbox.markAllRead')}
        </button>
      </div>

      {shown.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-[12px] text-muted">
          {t('workbench.inbox.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {shown.map((s) => {
            const unread = unreadOf(s);
            const projectLabel = s.project_name || s.project_id || 'avibe';
            return (
              <button
                key={s.session_id}
                type="button"
                onClick={() => onItemClick(s)}
                className={clsx(
                  'flex flex-col gap-1.5 rounded-lg px-3 py-2.5 text-left transition',
                  unread > 0
                    ? 'border-l-2 border-mint bg-mint/[0.06] hover:bg-mint/[0.10]'
                    : 'hover:bg-foreground/[0.04]',
                )}
              >
                <div className="flex items-center gap-1.5 text-[10px]">
                  <span className="truncate font-semibold text-cyan">{projectLabel}</span>
                  <span className="text-muted">·</span>
                  <span className="flex-1 truncate font-semibold text-foreground">
                    {s.title?.trim() || s.session_id}
                  </span>
                  {s.replied && (
                    <span className="shrink-0 font-semibold text-cyan" title={t('workbench.inbox.replied')}>
                      ↩
                    </span>
                  )}
                  <span className="font-mono text-muted">{formatRelativeTime(s.last_activity_at, t)}</span>
                </div>
                {s.preview_text ? (
                  <div
                    className={clsx(
                      'line-clamp-2 text-[11.5px] leading-relaxed',
                      unread > 0 ? 'text-foreground' : 'text-muted',
                    )}
                  >
                    <Markdown content={s.preview_text} interactive={false} className="vr-markdown--preview" />
                  </div>
                ) : (
                  <div className="text-[11.5px] leading-relaxed text-muted">—</div>
                )}
              </button>
            );
          })}
        </div>
      )}

      <button
        type="button"
        onClick={() => navigate('/inbox')}
        className="flex items-center justify-center gap-1.5 rounded-md pt-1 text-[11px] font-medium text-cyan hover:underline"
      >
        {t('workbench.inbox.viewAll')}
        <ArrowRight className="size-3" />
      </button>
    </div>
  );
};

// Session status dot colours. Maps the agent-runtime status to the user's
// gray / green / red: idle → muted (gray), running → mint (green) + glow,
// failed → destructive (red) + glow. Tokens resolve from src/index.css.
const STATUS_DOT_CLASS: Record<string, string> = {
  running: 'bg-mint shadow-[0_0_6px_0_rgba(91,255,160,0.65)]',
  failed: 'bg-destructive shadow-[0_0_6px_0_rgba(255,107,107,0.6)]',
  idle: 'bg-muted',
};

// One session row under a project. Left-click opens the chat; right-click opens
// the session action menu. Rename calls api.updateSession({ title }); the live
// session.activity 'updated' event then patches the title in this list (see the
// onSessionActivity handler in WorkbenchSidebar), so no manual local patch here.
const SessionRow: React.FC<{
  session: WorkbenchSession;
  unread: number;
  onSessionMarkRead: (sessionId: string) => void;
  onForkSession: (sessionId: string) => Promise<WorkbenchSession | null>;
  onRenameSession: (sessionId: string, title: string) => Promise<void>;
  onArchiveSession: (sessionId: string) => Promise<void>;
}> = ({ session, unread, onSessionMarkRead, onForkSession, onRenameSession, onArchiveSession }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const active = location.pathname === `/chat/${session.id}`;
  // "Reference this session" shows only when a chat composer is mounted (a chat
  // is open) AND this row isn't that open session — you can't reference yourself.
  const insertTarget = useComposerInsertTarget();
  const canReference = insertTarget != null && insertTarget.sessionId !== session.id;
  const [menuOpen, setMenuOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [forking, setForking] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title ?? '');
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Guards a double commit: Enter (or click-away) commits, then the input
  // unmounts and its onBlur would fire commitRename again; Escape cancels and
  // must NOT let that trailing blur commit the stale draft (Codex P2).
  const handledRef = useRef(false);

  useEffect(() => {
    if (renaming) inputRef.current?.focus();
  }, [renaming]);

  const commitRename = async () => {
    if (handledRef.current) return;
    handledRef.current = true;
    const trimmed = draft.trim();
    setRenaming(false);
    // No-op when unchanged; an empty name clears to "untitled" like the header.
    if (trimmed === (session.title ?? '').trim()) return;
    try {
      await onRenameSession(session.id, trimmed);
    } catch {
      // The shared apiFetch layer already surfaced the error toast.
    }
  };

  const cancelRename = () => {
    handledRef.current = true; // suppress the input's trailing onBlur commit
    setRenaming(false);
  };

  if (renaming) {
    return (
      <div className="flex items-center gap-2 py-1.5 pl-[30px] pr-2.5">
        <span
          className={clsx(
            'size-[5px] shrink-0 rounded-full',
            STATUS_DOT_CLASS[session.agent_status] ?? STATUS_DOT_CLASS.idle,
          )}
        />
        <Input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitRename();
            if (e.key === 'Escape') cancelRename();
          }}
          placeholder={t('workbench.sessionRenamePlaceholder')}
          className="h-7 flex-1 px-1.5 text-[12px]"
        />
      </div>
    );
  }

  const displayName = session.title?.trim() || t('workbench.untitledSession');
  const canFork = !!session.native_session_id && !forking;
  return (
    <>
    <Popover open={menuOpen} onOpenChange={setMenuOpen}>
      <PopoverAnchor asChild>
        <button
          type="button"
          onClick={() => {
            navigate(`/chat/${encodeURIComponent(session.id)}`);
            if (unread > 0) onSessionMarkRead(session.id);
          }}
          onContextMenu={(e) => {
            e.preventDefault();
            setMenuOpen(true);
          }}
          className={clsx(
            'group/sess flex items-center gap-2 rounded-md py-1.5 pl-[30px] pr-2.5 text-left transition',
            active
              ? 'border-l-2 border-mint bg-mint-soft pl-[28px] font-semibold text-foreground'
              : 'hover:bg-foreground/[0.04]',
          )}
        >
          <span
            title={t(`workbench.sessionStatus.${session.agent_status}`)}
            className={clsx(
              'size-[5px] shrink-0 rounded-full',
              STATUS_DOT_CLASS[session.agent_status] ?? STATUS_DOT_CLASS.idle,
            )}
          />
          <span
            className={clsx(
              'flex-1 truncate text-[12px]',
              active ? 'font-semibold text-foreground' : 'font-medium text-foreground',
            )}
          >
            {displayName}
          </span>
          {unread > 0 && (
            <span className="inline-flex min-w-[1.1rem] items-center justify-center rounded-full bg-mint px-1.5 font-mono text-[9px] font-bold text-[#080812]">
              {unread > 99 ? '99+' : unread}
            </span>
          )}
        </button>
      </PopoverAnchor>
      <PopoverContent align="start" className="w-[176px] p-1">
        {canReference && (
          <button
            type="button"
            onClick={() => {
              setMenuOpen(false);
              insertTarget?.insertSessionReference(session.id, session.title);
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
          >
            <Hash className="size-3 text-muted" />
            {t('workbench.sessionReference')}
          </button>
        )}
        <button
          type="button"
          disabled={!canFork}
          title={!session.native_session_id ? t('workbench.sessionForkUnavailable') : undefined}
          onClick={async () => {
            if (!canFork) return;
            setMenuOpen(false);
            setForking(true);
            try {
              const forked = await onForkSession(session.id);
              if (forked) navigate(`/chat/${encodeURIComponent(forked.id)}`);
            } finally {
              setForking(false);
            }
          }}
          className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04] disabled:cursor-not-allowed disabled:text-muted disabled:hover:bg-transparent"
        >
          {forking ? <Loader2 className="size-3 animate-spin text-muted" /> : <GitFork className="size-3 text-muted" />}
          {t('workbench.sessionFork')}
        </button>
        <button
          type="button"
          onClick={() => {
            setMenuOpen(false);
            setDraft(session.title ?? '');
            handledRef.current = false;
            setRenaming(true);
          }}
          className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
        >
          <Pencil className="size-3 text-muted" />
          {t('workbench.sessionRename')}
        </button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setMenuOpen(false);
            setArchiveOpen(true);
          }}
          className="h-auto w-full justify-start gap-2 rounded px-2 py-1.5 text-left text-[12px] font-normal text-pink hover:bg-pink/[0.08] hover:text-pink"
        >
          <Archive className="size-3" />
          {t('workbench.sessionArchive')}
        </Button>
      </PopoverContent>
    </Popover>
    <ArchiveSessionDialog
      sessionId={archiveOpen ? session.id : null}
      sessionTitle={session.title}
      open={archiveOpen}
      onOpenChange={setArchiveOpen}
      onConfirm={async () => {
        await onArchiveSession(session.id);
        // If we're viewing the chat we just archived, leave it directly — don't
        // rely solely on the replay-less SSE 'archived' event to navigate away.
        if (active) navigate('/inbox');
      }}
    />
    </>
  );
};

// One project row + (when expanded) the session list under it. Mirrors
// design.pen N96dsm/C68Ul (project row) and C7clY/R2C8U (session row).
const ProjectRow: React.FC<{
  project: WorkbenchProject;
  expanded: boolean;
  sessions: WorkbenchSession[] | null;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
  onToggle: () => void;
  onCreateSession: () => void;
  creatingSession: boolean;
  unreadBySession: Record<string, number>;
  onSessionMarkRead: (sessionId: string) => void;
  onRename: (next: string) => Promise<void>;
  onArchive: () => Promise<void>;
  onForkSession: (sessionId: string) => Promise<WorkbenchSession | null>;
  onRenameSession: (sessionId: string, title: string) => Promise<void>;
  onArchiveSession: (sessionId: string) => Promise<void>;
}> = ({
  project,
  expanded,
  sessions,
  loading,
  loadingMore,
  hasMore,
  onLoadMore,
  onToggle,
  onCreateSession,
  creatingSession,
  unreadBySession,
  onSessionMarkRead,
  onRename,
  onArchive,
  onForkSession,
  onRenameSession,
  onArchiveSession,
}) => {
  const { t } = useTranslation();
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState(project.display_name);
  const [menuOpen, setMenuOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentsMdOpen, setAgentsMdOpen] = useState(false);
  const renameInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (renaming) renameInputRef.current?.focus();
  }, [renaming]);

  const commitRename = async () => {
    const trimmed = renameDraft.trim();
    if (!trimmed || trimmed === project.display_name) {
      setRenaming(false);
      setRenameDraft(project.display_name);
      return;
    }
    await onRename(trimmed);
    setRenaming(false);
  };

  return (
    <div className="flex flex-col gap-0.5">
      <div
        className="group flex items-center gap-1.5 rounded-md py-1.5 pl-1 pr-2 transition hover:bg-foreground/[0.04]"
        title={project.folder_path}
        onContextMenu={(e) => {
          // Right-click opens the same menu as the ⋯ button (anchored to it).
          if (renaming) return;
          e.preventDefault();
          setMenuOpen(true);
        }}
      >
        {renaming ? (
          <div className="flex flex-1 items-center gap-1.5">
            {expanded ? (
              <FolderOpen className="size-3.5 shrink-0 text-muted" />
            ) : (
              <Folder className="size-3.5 shrink-0 text-muted" />
            )}
            <Input
              ref={renameInputRef}
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename();
                if (e.key === 'Escape') {
                  setRenameDraft(project.display_name);
                  setRenaming(false);
                }
              }}
              placeholder={t('workbench.projectRenamePlaceholder')}
              className="h-7 flex-1 px-1.5 text-[12px] font-medium"
            />
          </div>
        ) : (
          <button
            type="button"
            onClick={onToggle}
            className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          >
            <Chevron className="size-3 shrink-0 text-muted" />
            {expanded ? (
              <FolderOpen className="size-3.5 shrink-0 text-muted" />
            ) : (
              <Folder className="size-3.5 shrink-0 text-muted" />
            )}
            <span className="flex-1 truncate text-[12px] font-medium text-foreground">
              {project.display_name}
            </span>
          </button>
        )}
        {!renaming && (
          <>
            <Popover open={menuOpen} onOpenChange={setMenuOpen}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  aria-label={t('workbench.projectActions')}
                  className={clsx(
                    'flex size-5 shrink-0 items-center justify-center rounded-md text-muted transition',
                    'opacity-0 group-hover:opacity-100 hover:text-foreground hover:bg-foreground/[0.06]',
                    menuOpen && 'opacity-100',
                  )}
                >
                  <Ellipsis className="size-3" />
                </button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-[160px] p-1">
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    setRenaming(true);
                    setRenameDraft(project.display_name);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
                >
                  <Pencil className="size-3 text-muted" />
                  {t('workbench.projectRename')}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    setSettingsOpen(true);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
                >
                  <Settings2 className="size-3 text-muted" />
                  {t('workbench.projectSettings')}
                </button>
                {project.folder_path && (
                  <button
                    type="button"
                    onClick={() => {
                      setMenuOpen(false);
                      setAgentsMdOpen(true);
                    }}
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
                  >
                    <FileText className="size-3 text-muted" />
                    {t('workbench.projectEditAgents')}
                  </button>
                )}
                <button
                  type="button"
                  onClick={async () => {
                    setMenuOpen(false);
                    const ok = window.confirm(
                      t('workbench.projectArchiveConfirm', { name: project.display_name }),
                    );
                    if (ok) await onArchive();
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-pink transition hover:bg-pink/[0.08]"
                >
                  <Archive className="size-3" />
                  {t('workbench.projectArchive')}
                </button>
              </PopoverContent>
            </Popover>
            <button
              type="button"
              aria-label={t('workbench.addSession')}
              onClick={onCreateSession}
              disabled={creatingSession}
              className={clsx(
                'flex size-5 shrink-0 items-center justify-center rounded-md text-muted transition',
                'opacity-0 group-hover:opacity-100 hover:text-foreground hover:bg-foreground/[0.06]',
                creatingSession && 'opacity-100',
              )}
            >
              {creatingSession ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />}
            </button>
          </>
        )}
      </div>

      {expanded && (
        <div className="flex flex-col gap-0.5 pb-0.5">
          {loading && sessions === null && (
            <div className="px-3 py-2 pl-[30px] text-[11px] italic text-muted">{t('workbench.sessionsLoading')}</div>
          )}
          {sessions !== null && sessions.length === 0 && !loading && (
            <div className="px-3 py-2 pl-[30px] text-[11px] italic text-muted">{t('workbench.sessionsEmpty')}</div>
          )}
          {sessions !== null &&
            sessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                unread={unreadBySession[session.id] || 0}
                onSessionMarkRead={onSessionMarkRead}
                onForkSession={onForkSession}
                onRenameSession={onRenameSession}
                onArchiveSession={onArchiveSession}
              />
            ))}
          {hasMore && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loadingMore}
              className="flex items-center gap-1.5 rounded-md py-1.5 pl-[30px] pr-2.5 text-left text-[11px] font-medium text-muted transition hover:bg-foreground/[0.04] hover:text-foreground disabled:cursor-default disabled:opacity-60"
            >
              {loadingMore ? <Loader2 className="size-3 animate-spin" /> : <ChevronDown className="size-3" />}
              {t('workbench.sessionsLoadMore')}
            </button>
          )}
        </div>
      )}

      <ProjectSettingsDialog
        project={project}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
      <ProjectAgentsMdDialog
        project={project}
        open={agentsMdOpen}
        onClose={() => setAgentsMdOpen(false)}
      />
    </div>
  );
};

export const WorkbenchSidebar: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { totalUnread, unreadSessions, inboxSessions, markRead, unreadBySession } = useWorkbenchInbox();
  // Projects/sessions tree — shared with the mobile ProjectsPage via the provider
  // (one EventSource + one cache, not a per-component reimplementation). The
  // sidebar owns only its inbox popover + the New Project dialog trigger.
  const {
    projects,
    projectsError,
    sessionsOf,
    isExpanded,
    toggleExpanded,
    loadMore,
    creatingSession,
    createSessionForProject,
    forkSession,
    renameProject,
    archiveProject,
    renameSession,
    archiveSession,
    upsertProjectToTop,
  } = useWorkbenchProjectsTree();
  const [popoverOpen, setPopoverOpen] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const [showNewProject, setShowNewProject] = useState(false);

  // Small open/close delays so the popover doesn't flicker as the cursor
  // brushes through the inbox row on its way somewhere else, and survives
  // the gap between the row and the popover body.
  const openPopover = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    setPopoverOpen(true);
  };
  const queueClose = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
    }
    closeTimer.current = window.setTimeout(() => {
      setPopoverOpen(false);
      closeTimer.current = null;
    }, 180);
  };
  useEffect(() => {
    return () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    };
  }, []);

  const onItemClick = (session: InboxSession) => {
    setPopoverOpen(false);
    navigate(`/chat/${encodeURIComponent(session.session_id)}`);
    if ((unreadBySession[session.session_id] ?? 0) > 0) markRead(session.session_id);
  };

  const onMarkAllRead = async () => {
    // Mark every session that still has unread agent replies. The unread map is
    // pagination-independent, so this clears sessions beyond the first page too.
    const ids = Object.entries(unreadBySession)
      .filter(([, n]) => (n || 0) > 0)
      .map(([id]) => id);
    await Promise.all(ids.map((id) => markRead(id)));
  };

  const badge = useMemo(() => {
    if (totalUnread <= 0) return null;
    return totalUnread > 99 ? '99+' : String(totalUnread);
  }, [totalUnread]);

  // Fill the sidebar column and cap its height so the project list (and only the
  // project list) scrolls; Inbox + Capabilities stay pinned. The Inbox hover
  // popover stays OUT of any overflow box below, so it is never clipped.
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {/* Inbox entry — hover opens the floating popover. */}
      <div
        className="relative"
        onMouseEnter={openPopover}
        onMouseLeave={queueClose}
      >
        <NavLink
          to="/inbox"
          className={({ isActive }) =>
            clsx(
              'group flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] font-semibold transition-colors',
              // Cyan active state per design.pen ze15A — mint is reserved
              // for sessions / projects so the two reads stay distinct.
              isActive
                ? 'border-cyan/40 bg-cyan-soft text-foreground shadow-[0_0_16px_-4px_rgba(63,224,229,0.5)]'
                : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
            )
          }
        >
          {({ isActive }) => (
            <>
              <Inbox className={clsx('size-4', isActive ? 'text-cyan' : 'text-foreground')} />
              <span className="flex-1">{t('workbench.nav.inbox')}</span>
              {badge && (
                <span className="inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-cyan px-1.5 py-0.5 font-mono text-[9px] font-bold text-[#080812] shadow-[0_0_10px_-2px_rgba(63,224,229,0.7)]">
                  {badge}
                </span>
              )}
              <ChevronRight className="size-3.5 text-muted opacity-0 transition-opacity group-hover:opacity-100" />
            </>
          )}
        </NavLink>
        <InboxHoverPopover
          visible={popoverOpen}
          sessions={inboxSessions}
          unreadBySession={unreadBySession}
          unreadSessions={unreadSessions}
          totalUnread={totalUnread}
          onItemClick={onItemClick}
          onMarkAllRead={onMarkAllRead}
          onMouseEnter={openPopover}
          onMouseLeave={queueClose}
        />
      </div>

      <div className="flex flex-col gap-2">
        <div className="px-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
          {t('workbench.capabilitiesLabel')}
        </div>
        <nav className="flex flex-col gap-0.5">
          {CAPABILITY_NAV.map(({ to, i18nKey, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                clsx(
                  'group flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-colors',
                  isActive
                    ? 'border border-mint/30 bg-mint/[0.08] text-foreground shadow-[0_0_16px_-4px_rgba(91,255,160,0.5)]'
                    : 'border border-transparent text-muted hover:bg-foreground/[0.04] hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className={clsx('size-4', isActive ? 'text-mint' : 'text-muted group-hover:text-foreground')} />
                  <span>{t(i18nKey)}</span>
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>

      {/* Projects section — design.pen b8wX2. Header row carries the
          "Projects" label on the left (matching the Capabilities label
          style) and the 22x22 add button on the right. */}
      <div className="flex min-h-0 flex-1 flex-col gap-1.5">
        <div className="flex items-center justify-between px-1">
          <span className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
            {t('workbench.projectsLabel')}
          </span>
          {/* Borderless ghost icon button (design-system Button) — bumped from
              a 22px bordered box to a roomier 28px tap target. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 shrink-0 text-muted hover:text-foreground"
            aria-label={t('workbench.addProject')}
            onClick={() => setShowNewProject(true)}
          >
            <FolderPlus className="size-4" />
          </Button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto pr-0.5">
          {projects === null && !projectsError && (
            <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-3 text-[11px] text-muted">
              <Loader2 className="size-3 animate-spin" />
              {t('workbench.projectsLoading')}
            </div>
          )}
          {projectsError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[11px] text-destructive">
              {t('workbench.projectsLoadError')}
            </div>
          )}
          {projects !== null && projects.length === 0 && (
            <div className="flex flex-col items-center gap-1.5 rounded-md border border-dashed border-border px-3 py-4 text-center">
              <Folder className="size-4 text-muted" />
              <div className="text-[11px] text-muted">{t('workbench.projectsEmpty')}</div>
            </div>
          )}
          {projects !== null &&
            projects.map((project) => {
              const state = sessionsOf(project.id);
              return (
                <ProjectRow
                  key={project.id}
                  project={project}
                  expanded={isExpanded(project.id)}
                  sessions={state.sessions}
                  loading={state.loading}
                  loadingMore={state.loadingMore}
                  hasMore={!!state.cursor}
                  onLoadMore={() => loadMore(project.id)}
                  onToggle={() => toggleExpanded(project.id)}
                  onCreateSession={async () => {
                    // The provider creates + caches; navigation stays here since
                    // it's mounted above the router.
                    const session = await createSessionForProject(project.id);
                    if (session) navigate(`/chat/${encodeURIComponent(session.id)}`);
                  }}
                  creatingSession={creatingSession(project.id)}
                  unreadBySession={unreadBySession}
                  onSessionMarkRead={markRead}
                  onRename={(next) => renameProject(project.id, next)}
                  onArchive={() => archiveProject(project.id)}
                  onForkSession={(sessionId) => forkSession(project.id, sessionId)}
                  onRenameSession={(sessionId, title) => renameSession(project.id, sessionId, title)}
                  onArchiveSession={(sessionId) => archiveSession(project.id, sessionId)}
                />
              );
            })}
        </div>
      </div>

      {showNewProject && (
        <NewProjectDialog
          onClose={() => setShowNewProject(false)}
          onCreated={(project) => {
            setShowNewProject(false);
            upsertProjectToTop(project);
          }}
        />
      )}
    </div>
  );
};

// Re-export for tests / future inbox-specific UIs.
export { InboxHoverPopover };
