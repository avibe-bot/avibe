import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Archive,
  ChevronDown,
  Ellipsis,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Pencil,
  Plus,
  RotateCw,
  Settings2,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useWorkbenchProjectsActions, useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import type { ProjectSessionsState } from '../../context/WorkbenchProjectsContext';
import type { WorkbenchProject, WorkbenchSession } from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import {
  appShellScrollElement,
  clearProjectVisibleCount,
  consumeInheritedProjectsListScroll,
  holdMobileProjectsListForChatReturn,
  inheritedProjectsListScroll,
  markMobileProjectsListRestored,
  readAppShellScrollTop,
  readMobileProjectsListSnapshot,
  rememberMobileProjectsListCounts,
  rememberMobileProjectsListOnPageLeave,
  rememberMobileProjectsListScroll,
  revealMoreVisibleCount,
  visibleSessionCountFor,
  writeAppShellScrollTop,
  type MobileProjectsVisibleCounts,
} from '../../lib/mobileProjectsListMemory';
import { canCreateLocalProject } from '../../lib/sessionInfo';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { BotStrip } from './bots/BotStrip';
import { PageSectionTitle } from './PageSectionTitle';
import { NewProjectDialog } from './NewProjectDialog';
import { ProjectAgentsMdDialog } from './ProjectAgentsMdDialog';
import { ProjectSettingsDialog } from './ProjectSettingsDialog';
import { mobileSessionActions } from './chatSessionActions';
import { SessionActionMenuContent, SessionActionsTrigger } from './sessionActions';
import { useSessionActions } from './useSessionActions';
import { SessionPinIndicator } from './SessionPinIndicator';
import { SESSION_STATUS_DOT_MOTION_CLASS } from './sessionRowLayout';
import { SortableProjectList, type ProjectDragHandle } from './SortableProjectList';
import { MobileProjectCardsSkeleton, MobileSessionRowsSkeleton, SkeletonHandoverRows } from './listSkeletons';
import { useSkeletonHandover } from '../ui/skeleton-handover';
import { FlipList } from '../ui/flip-list';
import { Presence } from '../ui/presence';
import { treeMotion } from './treeMotion';

// Same status → colour mapping the desktop sidebar draws, and the same pulse
// while the agent is running; the two surfaces share the motion class so a
// running session reads the same way on a phone as on a desktop.
const DOT: Record<string, string> = {
  running: `bg-mint shadow-glow-dot-mint ${SESSION_STATUS_DOT_MOTION_CLASS}`,
  failed: 'bg-destructive',
  idle: 'bg-muted',
};

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
      danger ? 'text-pink-ink hover:bg-pink/[0.08] hover:text-pink-ink' : 'text-foreground hover:bg-wash',
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
  onLeaveToChat: () => void;
  dragHandle?: ProjectDragHandle;
}> = ({ project, open, state, onToggle, onLeaveToChat, dragHandle }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { capabilities } = useInstanceAuthorization();
  const canManageProjects = capabilities.can_manage_projects;
  const canArchive = canManageProjects;
  const canEditAgentsMd = canManageProjects;
  const { renameProject, archiveProject, createSessionForProject } = useWorkbenchProjectsActions();
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
  const canChat = capabilities.can_chat && project.capabilities.can_chat;
  // The row's ＋ and the ⋯ menu's New session both start one here.
  const startSession = async () => {
    if (creatingSessionRef.current) return;
    creatingSessionRef.current = true;
    try {
      const session = await createSessionForProject(project.id);
      if (session) {
        onLeaveToChat();
        navigate(`/chat/${encodeURIComponent(session.id)}`);
      }
    } finally {
      creatingSessionRef.current = false;
    }
  };

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
          {...dragHandle}
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="project-drag-header flex min-w-0 flex-1 items-center gap-2.5 px-4 py-3.5 text-left"
        >
          {/* The whole row opens the project, and the folder opens with it: no chevron. */}
          {open ? <FolderOpen className="project-drag-icon size-4 shrink-0 text-cyan-ink" /> : <Folder className="project-drag-icon size-4 shrink-0 text-muted" />}
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">{project.display_name}</span>
          {state.sessions !== null && !state.error && (
            // No taller than the name's line, so the row keeps its height when the count arrives.
            <Badge variant="secondary" className="font-mono text-[10px] leading-[14px]">
              {state.sessions.length}
              {state.cursor ? '+' : ''}
            </Badge>
          )}
        </button>
        {(canChat || canManageProjects) && <Popover open={menuOpen} onOpenChange={setMenuOpen}>
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
            {/* New session — first item, as well as the row's ＋ beside this
                menu (ui/design: mobile/workbench-tab/team). Desktop surfaces it as
                the sidebar row's "+", so it stays out of the desktop project menu. */}
            {canChat && <MenuItem
              icon={Plus}
              onClick={() => {
                setMenuOpen(false);
                void startSession();
              }}
            >
              {t('newSession.title')}
            </MenuItem>}
            {canManageProjects && <>
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
            {project.folder_path && canEditAgentsMd && (
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
            {canArchive && <MenuItem
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
            </MenuItem>}
            </>}
          </PopoverContent>
        </Popover>}
        {canChat && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => void startSession()}
            aria-label={t('projects.newSessionIn', { name: project.display_name })}
            className="size-8 shrink-0 text-muted hover:text-foreground"
          >
            <Plus className="size-4" />
          </Button>
        )}
      </div>
      {canManageProjects && (
        <>
          <ProjectSettingsDialog project={project} open={settingsOpen} onClose={() => setSettingsOpen(false)} />
          <ProjectAgentsMdDialog project={project} open={agentsOpen} onClose={() => setAgentsOpen(false)} />
        </>
      )}
    </>
  );
};

// Session row: tap to open the chat, ⋯ for the shared session action menu (the
// same items the desktop sidebar and the chat header show — see sessionActions.tsx).
// Rename is inline, keyboard-safe on iOS for the same reason as the project row.
const MobileSessionRow: React.FC<{
  projectId: string;
  session: WorkbenchSession;
  unread: number;
  canChat: boolean;
  canManageMetadata: boolean;
  onOpen: () => void;
  onLeaveToChat: () => void;
}> = ({ projectId, session, unread, canChat, canManageMetadata, onOpen, onLeaveToChat }) => {
  const { t } = useTranslation();
  const { renameSession } = useWorkbenchProjectsActions();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title ?? '');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const handledRef = useRef(false);

  const { actions, archiveDialog } = useSessionActions({
    session,
    writable: canManageMetadata,
    lifecycleWritable: canChat,
    projectId,
    onRenameStart: () => {
      setDraft(session.title ?? '');
      handledRef.current = false;
      setRenaming(true);
    },
    onOpenSession: (sessionId) => {
      onLeaveToChat();
      navigate(`/chat/${encodeURIComponent(sessionId)}`);
    },
  });

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
      <div data-flip-id={session.id} className="flex items-center gap-2 px-3 py-2">
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
    <div data-flip-id={session.id} className="group/sess flex items-center">
      <button
        type="button"
        onClick={onOpen}
        className="press press-surface flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-3 py-2.5 text-left hover:bg-wash active:bg-wash-press"
      >
        <span className={clsx('size-1.5 shrink-0 rounded-full', DOT[session.agent_status] ?? DOT.idle)} />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
          {session.title || `#${session.id.slice(-6)}`}
        </span>
        <SessionPinIndicator pinned={session.pinned} label={t('workbench.sessionPinned')} />
        {/* Read: the count shrinks away as the time fades in (M7). */}
        <Presence show={unread > 0} value={unread}>
          {(count) => (
            <span className="shrink-0 rounded-full bg-mint px-1.5 py-0.5 font-mono text-[10px] font-bold text-primary-foreground">
              {count > 99 ? '99+' : count}
            </span>
          )}
        </Presence>
        <Presence show={unread === 0} kind="fade">
          {() => (
            <span className="shrink-0 text-[10.5px] text-muted">
              {formatRelativeTime(session.last_active_at ?? session.updated_at, t)}
            </span>
          )}
        </Presence>
      </button>
      {canManageMetadata && <Popover open={menuOpen} onOpenChange={setMenuOpen}>
        <PopoverTrigger asChild>
          <SessionActionsTrigger
            label={t('workbench.sessionActions')}
            open={menuOpen}
            variant="bar"
            className="size-8"
          />
        </PopoverTrigger>
        <SessionActionMenuContent
          actions={mobileSessionActions(actions)}
          label={t('workbench.sessionActions')}
          align="end"
          onClose={() => setMenuOpen(false)}
        />
      </Popover>}
      {archiveDialog}
    </div>
  );
};

// Mobile-only Workbench tab: the desktop sidebar on a page — the Owner's Bots strip
// first, then the projects tree flattened into a full-page accordion (ui/design:
// mobile/workbench-tab/team). Tap a project to expand its sessions, tap a session
// to open the chat, ＋ to start a Session there, ⋯ for the rename/archive/
// edit-AGENTS.md actions the desktop exposes via right-click + hover. Shares the
// same data provider (useWorkbenchProjectsTree) as the sidebar (ui/design: mobile/projects/expanded).
export const ProjectsPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    capabilities,
  } = useInstanceAuthorization();
  const canCreateProject = canCreateLocalProject(capabilities);
  // Per-session dots only; the feed list itself lives in the sidebar / Inbox.
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const {
    projects,
    projectsLoaded,
    projectsError,
    refreshProjects,
    sessionsOf,
    isExpanded,
    toggleExpanded,
    loadMore,
    reloadSessions,
    reorderProjects,
    isReorderingProjects,
  } = useWorkbenchProjectsTree();
  const [showNewProject, setShowNewProject] = useState(false);
  const [visibleSessionCounts, setVisibleSessionCounts] = useState<MobileProjectsVisibleCounts>(
    () => readMobileProjectsListSnapshot().visibleCounts,
  );
  // Decided once, on the first render: the restore below can consume the
  // inherited place before the effect that reads this runs.
  const [inheritedOnMount] = useState(() => inheritedProjectsListScroll() !== null);
  useEffect(() => {
    // A list this document showed before (a conversation and back) is drawn at
    // once from the provider, so its place is put back now. One a previous
    // document left waits for its rows instead (below).
    const inherited = inheritedOnMount;
    const top = readMobileProjectsListSnapshot().scrollTop;
    const restore = () => writeAppShellScrollTop(appShellScrollElement(), top);
    if (!inherited) restore();
    const frame = inherited ? 0 : requestAnimationFrame(restore);
    markMobileProjectsListRestored();
    const scrollEl = appShellScrollElement();
    const onScroll = () => rememberMobileProjectsListScroll(readAppShellScrollTop(scrollEl));
    scrollEl?.addEventListener?.('scroll', onScroll, { passive: true });
    // The reader's own scrolling outranks a place still waiting to be restored.
    const giveUpRestore = () => consumeInheritedProjectsListScroll();
    const inputs = ['wheel', 'touchmove', 'keydown'] as const;
    if (inherited) for (const type of inputs) window.addEventListener(type, giveUpRestore, { capture: true, passive: true });
    return () => {
      cancelAnimationFrame(frame);
      scrollEl?.removeEventListener?.('scroll', onScroll);
      for (const type of inputs) window.removeEventListener(type, giveUpRestore, { capture: true });
    };
  }, [inheritedOnMount]);

  // The place a previous document left the list at, written once, before the
  // first frame that can hold it. The projects it left open are open from the
  // first render that has the list, but one whose rows were not stored draws a
  // shorter skeleton until they come, so a place near the end waits for them.
  // Once the list has settled (live, and no open project still waiting), the
  // place is written even if the list is shorter now: the nearest it has.
  const listShown = projects !== null;
  const listSettled = listShown && projectsLoaded && !(projects ?? []).some((project) => {
    if (!isExpanded(project.id)) return false;
    const state = sessionsOf(project.id);
    return state.loading && !state.sessions?.length;
  });
  useLayoutEffect(() => {
    if (!listShown) return;
    const top = inheritedProjectsListScroll();
    if (top === null) return;
    const scroller = appShellScrollElement();
    const room = (scroller?.scrollHeight ?? 0) - (scroller?.clientHeight ?? 0);
    if (room < top && !listSettled) return;
    consumeInheritedProjectsListScroll();
    writeAppShellScrollTop(scroller, top);
  });

  useEffect(() => {
    rememberMobileProjectsListCounts(visibleSessionCounts);
    return () => {
      rememberMobileProjectsListOnPageLeave({
        visibleCounts: visibleSessionCounts,
        scrollTop: readMobileProjectsListSnapshot().scrollTop,
      });
    };
  }, [visibleSessionCounts]);

  const captureListForChatReturn = () => {
    holdMobileProjectsListForChatReturn({
      visibleCounts: visibleSessionCounts,
      scrollTop: readAppShellScrollTop(appShellScrollElement()),
    });
  };

  const openSession = (sessionId: string) => {
    captureListForChatReturn();
    navigate(`/chat/${encodeURIComponent(sessionId)}`);
  };
  const toggleProject = (projectId: string) => {
    const willCollapse = isExpanded(projectId);
    if (willCollapse) {
      setVisibleSessionCounts((prev) => clearProjectVisibleCount(prev, projectId));
    }
    toggleExpanded(projectId);
  };
  const revealMoreSessions = (projectId: string, state: ProjectSessionsState, visibleCount: number, loadedCount: number) => {
    setVisibleSessionCounts((prev) => ({
      ...prev,
      [projectId]: revealMoreVisibleCount(visibleCount),
    }));
    if (loadedCount <= visibleCount && state.cursor) {
      loadMore(projectId);
    }
  };

  const list = projects ?? [];
  // The list's skeleton, and the fade its cards take where it was.
  const projectsSkeleton = projects === null && !projectsError;
  const projectsHandover = useSkeletonHandover(projectsSkeleton);
  // The tree as its motion follows it (M7; see `treeMotion`).
  const tree = treeMotion(projects, (projectId) => {
    const open = isExpanded(projectId);
    const state = sessionsOf(projectId);
    const all = state.sessions ?? [];
    const shown = visibleSessionCountFor(visibleSessionCounts, projectId);
    return {
      open,
      rows: state.loading && all.length === 0 ? null : all.slice(0, shown),
      more: (all.length > shown || !!state.cursor) && !state.loadingMore,
    };
  });
  // New project sits at the end of the Projects title's row, the same height as the title.
  const newProjectButton = canCreateProject ? (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={() => setShowNewProject(true)}
      aria-label={t('projects.newProject')}
      title={t('projects.newProject')}
      className="size-8 text-muted hover:text-foreground"
    >
      <FolderPlus className="size-[18px]" />
    </Button>
  ) : null;

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6">
    {/* The strip draws nothing for a reader without Bots. Both sections are titled
        alike (PageSectionTitle). */}
    <BotStrip unreadBySession={unreadBySession} placement="page" />
    <div className="flex flex-col gap-2">
      <PageSectionTitle title={t('projects.title')} action={newProjectButton} />

      {/* The provider often has projects cached already (it's mounted app-wide),
          so this loading flash only shows on a genuine cold start. */}
      {projectsSkeleton && <MobileProjectCardsSkeleton rows={4} />}
      {/* Also over the last known list painted from the local snapshot: those
          rows are not a load, so a failed one still offers its retry. */}
      {projectsError && (list.length === 0 || !projectsLoaded) && (
        <button
          type="button"
          onClick={() => void refreshProjects()}
          className="flex items-center justify-center gap-2 rounded-xl border border-edge bg-surface px-4 py-10 text-sm text-muted transition hover:text-foreground shadow-elevation-0"
        >
          <RotateCw className="size-4" />
          {t('projects.loadFailed')}
        </button>
      )}
      {projects !== null && list.length === 0 && !projectsError && (
        <div className="rounded-xl border border-edge bg-surface px-4 py-10 text-center text-sm text-muted shadow-elevation-0">
          {t('projects.empty')}
        </div>
      )}

      {list.length > 0 && <FlipList ids={tree.ids} group={tree.group} className="flex flex-col gap-2">
      <SortableProjectList projects={list} mobile
        disabled={!capabilities.can_manage_projects || isReorderingProjects}
        onReorder={reorderProjects}
      >
      {(project, dragHandle) => {
        const open = isExpanded(project.id);
        const state = sessionsOf(project.id);
        const allSessionRows = state.sessions ?? [];
        const visibleSessionCount = visibleSessionCountFor(visibleSessionCounts, project.id);
        const sessionRows = allSessionRows.slice(0, visibleSessionCount);
        const hasHiddenCachedSessions = allSessionRows.length > visibleSessionCount;
        const hasMoreSessions = hasHiddenCachedSessions || !!state.cursor;
        return (
          <div key={project.id} data-flip-id={`project:${project.id}`} className={clsx('overflow-hidden rounded-xl border border-edge bg-surface shadow-elevation-0', projectsHandover)}>
            <MobileProjectRow
              dragHandle={dragHandle}
              project={project}
              open={open}
              state={state}
              onToggle={() => toggleProject(project.id)}
              onLeaveToChat={captureListForChatReturn}
            />

            {open && (
              <div className="flex flex-col gap-0.5 border-t border-border px-2 py-2">
                {state.loading && allSessionRows.length === 0 && <MobileSessionRowsSkeleton rows={3} />}
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
                <SkeletonHandoverRows skeleton={state.loading && allSessionRows.length === 0} className="flex flex-col gap-0.5">
                  {sessionRows.map((session) => (
                    <MobileSessionRow
                      key={session.id}
                      projectId={project.id}
                      session={session}
                      unread={unreadBySession[session.id] ?? 0}
                      canChat={capabilities.can_chat && project.capabilities.can_chat}
                      canManageMetadata={capabilities.can_manage_projects || project.capabilities.can_chat}
                      onOpen={() => openSession(session.id)}
                      onLeaveToChat={captureListForChatReturn}
                    />
                  ))}
                </SkeletonHandoverRows>
                {hasMoreSessions && state.loadingMore && <MobileSessionRowsSkeleton rows={2} />}
                {hasMoreSessions && !state.loadingMore && (
                  <button
                    type="button"
                    data-flip-id={`more:${project.id}`}
                    onClick={() => revealMoreSessions(project.id, state, visibleSessionCount, allSessionRows.length)}
                    className="flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-[12px] font-medium text-cyan-ink transition hover:bg-cyan/[0.06]"
                  >
                    <ChevronDown className="size-3.5" />
                    {t('projects.loadMore')}
                  </button>
                )}
              </div>
            )}
          </div>
        );
      }}
      </SortableProjectList>
      </FlipList>}

      {showNewProject && canCreateProject && (
        <NewProjectDialog
          onClose={() => setShowNewProject(false)}
          onCreated={() => setShowNewProject(false)}
        />
      )}
    </div>
    </div>
  );
};
