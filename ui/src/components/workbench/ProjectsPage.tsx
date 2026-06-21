import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Ellipsis,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  GitFork,
  Loader2,
  Pencil,
  Plus,
  RotateCw,
  Settings2,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import type { ProjectSessionsState } from '../../context/WorkbenchProjectsContext';
import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { ArchiveSessionDialog } from './ArchiveSessionDialog';
import { NewProjectDialog } from './NewProjectDialog';
import { ProjectAgentsMdDialog } from './ProjectAgentsMdDialog';
import { ProjectSettingsDialog } from './ProjectSettingsDialog';

const DOT: Record<string, string> = {
  running: 'bg-mint shadow-[0_0_7px_rgba(91,255,160,0.9)]',
  failed: 'bg-destructive',
  idle: 'bg-muted',
};
const MOBILE_SESSION_PAGE_SIZE = 8;

// One row in a ⋯ popover menu, on the design-system Button idiom (plain button to
// match the desktop sidebar menus). `danger` tints destructive actions (archive).
const MenuItem: React.FC<{ icon: LucideIcon; onClick: () => void; danger?: boolean; children: React.ReactNode }> = ({
  icon: Icon,
  onClick,
  danger,
  children,
}) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    onClick={onClick}
    className={clsx(
      'h-auto w-full justify-start gap-2 rounded px-2 py-2 text-left text-[13px] font-normal',
      danger ? 'text-pink hover:bg-pink/[0.08] hover:text-pink' : 'text-foreground hover:bg-foreground/[0.04]',
    )}
  >
    <Icon className={clsx('size-3.5 shrink-0', danger ? '' : 'text-muted')} />
    {children}
  </Button>
);

// Project header row: tap to expand, ⋯ for the actions the desktop sidebar
// exposes via right-click / hover (Rename / Edit AGENTS.md / Archive). Touch has
// no right-click, so the menu is an always-visible ⋯. Rename is INLINE (an
// in-flow Input) rather than a bottom-sheet dialog, so iOS scrolls it above the
// keyboard instead of stranding it behind. Actions reuse the shared provider.
const MobileProjectRow: React.FC<{
  project: WorkbenchProject;
  open: boolean;
  state: ProjectSessionsState;
  onToggle: () => void;
}> = ({ project, open, state, onToggle }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { renameProject, archiveProject, createSessionForProject } = useWorkbenchProjectsTree();
  const [menuOpen, setMenuOpen] = useState(false);
  // Guards against a double-tap creating two sessions before navigation unmounts.
  const creatingSessionRef = useRef(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(project.display_name);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentsOpen, setAgentsOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Enter (or blur) commits, then the input unmounts and its blur fires again;
  // Escape cancels and must not let that trailing blur commit the stale draft.
  const handledRef = useRef(false);

  useEffect(() => {
    if (renaming) inputRef.current?.focus();
  }, [renaming]);

  const commitRename = async () => {
    if (handledRef.current) return;
    handledRef.current = true;
    const trimmed = draft.trim();
    setRenaming(false);
    if (!trimmed || trimmed === project.display_name) {
      setDraft(project.display_name);
      return;
    }
    try {
      await renameProject(project.id, trimmed);
    } catch {
      // apiFetch already surfaced the error toast.
    }
  };
  const cancelRename = () => {
    handledRef.current = true;
    setRenaming(false);
    setDraft(project.display_name);
  };

  if (renaming) {
    return (
      <div className="flex items-center gap-2 px-4 py-3">
        <Folder className="size-4 shrink-0 text-muted" />
        <Input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitRename();
            if (e.key === 'Escape') cancelRename();
          }}
          placeholder={t('workbench.projectRenamePlaceholder')}
          className="h-8 flex-1 text-sm font-medium"
        />
      </div>
    );
  }

  return (
    <>
      <div className="flex items-center pr-1.5">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2.5 px-4 py-3.5 text-left"
        >
          {open ? <FolderOpen className="size-4 shrink-0 text-cyan" /> : <Folder className="size-4 shrink-0 text-muted" />}
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">{project.display_name}</span>
          {state.sessions !== null && !state.error && (
            <Badge variant="secondary" className="font-mono text-[10px]">
              {state.sessions.length}
              {state.cursor ? '+' : ''}
            </Badge>
          )}
          {open ? <ChevronDown className="size-4 shrink-0 text-muted" /> : <ChevronRight className="size-4 shrink-0 text-muted" />}
        </button>
        <Popover open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={t('workbench.projectActions')}
              className="size-8 shrink-0 text-muted"
            >
              <Ellipsis className="size-4" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-[200px] p-1">
            {/* New session — first item, mobile only. Desktop surfaces this as
                the per-project "+" button in the sidebar, so it stays out of the
                desktop project menu; here the "+" doesn't fit, so it lives here. */}
            <MenuItem
              icon={Plus}
              onClick={async () => {
                setMenuOpen(false);
                if (creatingSessionRef.current) return;
                creatingSessionRef.current = true;
                try {
                  const session = await createSessionForProject(project.id);
                  if (session) navigate(`/chat/${encodeURIComponent(session.id)}`);
                } finally {
                  creatingSessionRef.current = false;
                }
              }}
            >
              {t('newSession.title')}
            </MenuItem>
            <MenuItem
              icon={Pencil}
              onClick={() => {
                setMenuOpen(false);
                setDraft(project.display_name);
                handledRef.current = false;
                setRenaming(true);
              }}
            >
              {t('workbench.projectRename')}
            </MenuItem>
            <MenuItem
              icon={Settings2}
              onClick={() => {
                setMenuOpen(false);
                setSettingsOpen(true);
              }}
            >
              {t('workbench.projectSettings')}
            </MenuItem>
            {project.folder_path && (
              <MenuItem
                icon={FileText}
                onClick={() => {
                  setMenuOpen(false);
                  setAgentsOpen(true);
                }}
              >
                {t('workbench.projectEditAgents')}
              </MenuItem>
            )}
            <MenuItem
              icon={Archive}
              danger
              onClick={async () => {
                setMenuOpen(false);
                if (window.confirm(t('workbench.projectArchiveConfirm', { name: project.display_name }))) {
                  await archiveProject(project.id);
                }
              }}
            >
              {t('workbench.projectArchive')}
            </MenuItem>
          </PopoverContent>
        </Popover>
      </div>
      <ProjectSettingsDialog project={project} open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <ProjectAgentsMdDialog project={project} open={agentsOpen} onClose={() => setAgentsOpen(false)} />
    </>
  );
};

// Session row: tap to open the chat, ⋯ to rename (the desktop sidebar exposes
// rename via right-click). Inline rename, same keyboard-safe reasoning as above.
const MobileSessionRow: React.FC<{
  projectId: string;
  session: WorkbenchSession;
  unread: number;
  onOpen: () => void;
}> = ({ projectId, session, unread, onOpen }) => {
  const { t } = useTranslation();
  const { renameSession, archiveSession, forkSession } = useWorkbenchProjectsTree();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title ?? '');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const handledRef = useRef(false);
  const forkingRef = useRef(false);

  useEffect(() => {
    if (renaming) inputRef.current?.focus();
  }, [renaming]);

  const commitRename = async () => {
    if (handledRef.current) return;
    handledRef.current = true;
    const trimmed = draft.trim();
    setRenaming(false);
    // No-op when unchanged; empty clears to "untitled" server-side (matches desktop).
    if (trimmed === (session.title ?? '').trim()) return;
    try {
      await renameSession(projectId, session.id, trimmed);
    } catch {
      // apiFetch already surfaced the error toast.
    }
  };
  const cancelRename = () => {
    handledRef.current = true;
    setRenaming(false);
  };

  if (renaming) {
    return (
      <div className="flex items-center gap-2 px-3 py-2">
        <span className={clsx('size-1.5 shrink-0 rounded-full', DOT[session.agent_status] ?? DOT.idle)} />
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
          className="h-7 flex-1 text-[13px]"
        />
      </div>
    );
  }

  return (
    <div className="flex items-center">
      <button
        type="button"
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-3 py-2.5 text-left transition hover:bg-foreground/[0.04]"
      >
        <span className={clsx('size-1.5 shrink-0 rounded-full', DOT[session.agent_status] ?? DOT.idle)} />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
          {session.title || `#${session.id.slice(-6)}`}
        </span>
        {unread > 0 ? (
          <span className="shrink-0 rounded-full bg-mint px-1.5 py-0.5 font-mono text-[10px] font-bold text-background">
            {unread > 99 ? '99+' : unread}
          </span>
        ) : (
          <span className="shrink-0 text-[10.5px] text-muted">
            {formatRelativeTime(session.last_active_at ?? session.updated_at, t)}
          </span>
        )}
      </button>
      <Popover open={menuOpen} onOpenChange={setMenuOpen}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t('workbench.sessionActions')}
            className="size-8 shrink-0 text-muted"
          >
            <Ellipsis className="size-3.5" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-[176px] p-1">
          <MenuItem
            icon={Pencil}
            onClick={() => {
              setMenuOpen(false);
              setDraft(session.title ?? '');
              handledRef.current = false;
              setRenaming(true);
            }}
          >
            {t('workbench.sessionRename')}
          </MenuItem>
          {/* Fork is hidden until the session has a native agent session to fork
              (mirrors the desktop sidebar's fork gate). */}
          {session.native_session_id && (
            <MenuItem
              icon={GitFork}
              onClick={async () => {
                setMenuOpen(false);
                // The row stays mounted after the menu closes, so guard against a
                // second tap (reopened menu) spawning a duplicate fork in flight.
                if (forkingRef.current) return;
                forkingRef.current = true;
                try {
                  const forked = await forkSession(projectId, session.id);
                  if (forked) navigate(`/chat/${encodeURIComponent(forked.id)}`);
                } finally {
                  forkingRef.current = false;
                }
              }}
            >
              {t('workbench.sessionFork')}
            </MenuItem>
          )}
          <MenuItem
            icon={Archive}
            danger
            onClick={() => {
              setMenuOpen(false);
              setArchiveOpen(true);
            }}
          >
            {t('workbench.sessionArchive')}
          </MenuItem>
        </PopoverContent>
      </Popover>
      <ArchiveSessionDialog
        sessionId={archiveOpen ? session.id : null}
        sessionTitle={session.title}
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        onConfirm={() => archiveSession(projectId, session.id)}
      />
    </div>
  );
};

// Mobile-only "Projects" tab (workbench): the desktop projects tree
// (WorkbenchSidebar) flattened into a full-page accordion — tap a project to
// expand its sessions, tap a session to open the chat, ⋯ for the rename/archive/
// edit-AGENTS.md actions the desktop exposes via right-click + hover. Shares the
// same data provider (useWorkbenchProjectsTree) as the sidebar. Design: `FW7cI`.
export const ProjectsPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { markRead, unreadBySession } = useWorkbenchInbox();
  const {
    projects,
    projectsError,
    refreshProjects,
    sessionsOf,
    isExpanded,
    toggleExpanded,
    loadMore,
    reloadSessions,
    upsertProjectToTop,
  } = useWorkbenchProjectsTree();
  const [showNewProject, setShowNewProject] = useState(false);
  const [visibleSessionCounts, setVisibleSessionCounts] = useState<Record<string, number>>({});

  const openSession = (sessionId: string) => {
    // Opening a chat marks it read everywhere (matches the desktop tree + Inbox),
    // so unread badges/counts don't linger after a mobile drill-in.
    void markRead(sessionId);
    navigate(`/chat/${sessionId}`);
  };
  const revealMoreSessions = (projectId: string, state: ProjectSessionsState, visibleCount: number, loadedCount: number) => {
    setVisibleSessionCounts((prev) => ({
      ...prev,
      [projectId]: visibleCount + MOBILE_SESSION_PAGE_SIZE,
    }));
    if (loadedCount <= visibleCount && state.cursor) {
      loadMore(projectId);
    }
  };

  const list = projects ?? [];

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-3">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">{t('projects.title')}</h1>
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={() => setShowNewProject(true)}
          aria-label={t('projects.newProject')}
          className="border-mint/35 bg-mint/[0.08] text-mint hover:bg-mint/[0.14]"
        >
          <FolderPlus className="size-4" />
        </Button>
      </div>

      {/* The provider often has projects cached already (it's mounted app-wide),
          so this loading flash only shows on a genuine cold start. */}
      {projects === null && !projectsError && (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-10 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('common.loading')}
        </div>
      )}
      {projectsError && list.length === 0 && (
        <button
          type="button"
          onClick={() => void refreshProjects()}
          className="flex items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-10 text-sm text-muted transition hover:text-foreground"
        >
          <RotateCw className="size-4" />
          {t('projects.loadFailed')}
        </button>
      )}
      {projects !== null && list.length === 0 && !projectsError && (
        <div className="rounded-xl border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
          {t('projects.empty')}
        </div>
      )}

      {list.map((project) => {
        const open = isExpanded(project.id);
        const state = sessionsOf(project.id);
        const allSessionRows = state.sessions ?? [];
        const visibleSessionCount = visibleSessionCounts[project.id] ?? MOBILE_SESSION_PAGE_SIZE;
        const sessionRows = allSessionRows.slice(0, visibleSessionCount);
        const hasHiddenCachedSessions = allSessionRows.length > visibleSessionCount;
        const hasMoreSessions = hasHiddenCachedSessions || !!state.cursor;
        return (
          <div key={project.id} className="overflow-hidden rounded-xl border border-border bg-surface">
            <MobileProjectRow project={project} open={open} state={state} onToggle={() => toggleExpanded(project.id)} />

            {open && (
              <div className="flex flex-col gap-0.5 border-t border-border px-2 py-2">
                {state.loading && allSessionRows.length === 0 && (
                  <div className="flex items-center justify-center gap-2 px-3 py-3 text-[13px] text-muted">
                    <Loader2 className="size-3.5 animate-spin" />
                    {t('common.loading')}
                  </div>
                )}
                {state.error && allSessionRows.length === 0 && (
                  <button
                    type="button"
                    onClick={() => reloadSessions(project.id)}
                    className="flex items-center justify-center gap-2 rounded-lg px-3 py-3 text-[13px] text-muted transition hover:text-foreground"
                  >
                    <RotateCw className="size-3.5" />
                    {t('projects.loadFailed')}
                  </button>
                )}
                {state.sessions !== null && allSessionRows.length === 0 && !state.loading && !state.error && (
                  <div className="px-3 py-3 text-center text-[13px] text-muted">{t('projects.noSessions')}</div>
                )}
                {sessionRows.map((session) => (
                  <MobileSessionRow
                    key={session.id}
                    projectId={project.id}
                    session={session}
                    unread={unreadBySession[session.id] ?? 0}
                    onOpen={() => openSession(session.id)}
                  />
                ))}
                {hasMoreSessions && (
                  <button
                    type="button"
                    onClick={() => revealMoreSessions(project.id, state, visibleSessionCount, allSessionRows.length)}
                    disabled={state.loadingMore}
                    className="flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-[12px] font-medium text-cyan transition hover:bg-cyan/[0.06] disabled:opacity-50"
                  >
                    {state.loadingMore ? <Loader2 className="size-3.5 animate-spin" /> : <ChevronDown className="size-3.5" />}
                    {t('projects.loadMore')}
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}

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
