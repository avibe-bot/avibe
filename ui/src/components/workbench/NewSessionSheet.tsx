import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useNewSession } from '../../lib/useNewSession';
import { useUnsavedChangesActionGuard } from '../../context/useUnsavedChangesActionGuard';
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog';
import { Composer } from './Composer';
import { NewProjectDialog } from './NewProjectDialog';
import { ProjectPicker } from './ProjectPicker';
import { AgentRoutePicker } from './AgentRoutePicker';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import type { UnsavedChangesActionAuthorization } from '../../lib/unsavedChangesRegistry';

interface NewSessionSheetProps {
  open: boolean;
  onClose: () => void;
  onOpen: () => void;
}

// The workbench center ＋ opens this instead of jumping to the home canvas.
// Pick a project (chips, most-recent first), describe the task, and it creates
// the session + submits the message before routing to /chat — the same flow as
// the desktop Workbench home, surfaced as a mobile bottom sheet (design.pen KSXXB).
// The create flow itself lives in the shared useNewSession hook (one source of
// truth with the home); the sheet only owns its open/close + draft lifecycle.
export const NewSessionSheet: React.FC<NewSessionSheetProps> = ({ open, onClose, onOpen }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const surfaceActive = useRouteSurfaceActive();
  const authorizeRouteAction = useUnsavedChangesActionGuard();
  // active: open → the hook reloads + resets per-open (the sheet is permanently
  // mounted by AppShell, so stale submit/error state must not leak across opens).
  const ns = useNewSession({
    active: open,
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
    errorText: t,
  });
  // This existing stash owns text while the modal's Composer is unmounted,
  // both for Settings suspension and the no-project handoff.
  const [pendingDraft, setPendingDraft] = useState('');
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [pendingNavigation, setPendingNavigation] = useState<{
    sessionId: string;
    authorization: UnsavedChangesActionAuthorization | undefined;
  } | null>(null);
  const [lifetime, setLifetime] = useState(0);
  const [previousOpen, setPreviousOpen] = useState(open);
  // A parent close invalidates old Composer callbacks too. The project detour
  // deliberately retains its prompt; Settings does not change logical open.
  if (previousOpen !== open) {
    setPreviousOpen(open);
    setLifetime(lifetime + 1);
    setPendingNavigation(null);
    if (!open && !newProjectOpen) setPendingDraft('');
  }
  const current = useRef({ open, surfaceActive, lifetime, mounted: true });
  useLayoutEffect(() => {
    current.current = { open, surfaceActive, lifetime, mounted: true };
    return () => { current.current.mounted = false; };
  }, [open, surfaceActive, lifetime]);
  const navigationClaim = useRef<typeof pendingNavigation>(null);
  const close = () => {
    if (!current.current.surfaceActive) return;
    setPendingNavigation(null);
    setPendingDraft('');
    onClose();
  };
  const updateDraft = (text: string) => {
    if (current.current.mounted && current.current.lifetime === lifetime) setPendingDraft(text);
  };

  // A completed POST is final even if Settings currently owns the foreground.
  // Retain that result, then consume it once with the latest navigator on
  // return. Never capture a pre-suspension navigator here.
  useEffect(() => {
    if (!open || !surfaceActive || !pendingNavigation || navigationClaim.current === pendingNavigation) return;
    navigationClaim.current = pendingNavigation;
    const { sessionId, authorization } = pendingNavigation;
    const navigateToSession = () => navigate(`/chat/${encodeURIComponent(sessionId)}`);
    if (authorization) authorization.runNavigation(navigateToSession);
    else navigateToSession();
    onClose();
  }, [navigate, onClose, open, pendingNavigation, surfaceActive]);

  // Close the sheet before opening a sibling project dialog so the old modal
  // cannot trap pointer/focus over the new folder picker.
  const openNewProject = () => {
    if (!current.current.surfaceActive || ns.sending || pendingNavigation) return;
    setNewProjectOpen(true);
    onClose();
  };

  const send = async (text: string): Promise<boolean> => {
    if (!current.current.open || !current.current.surfaceActive || pendingNavigation) return false;
    // Composer clears optimistically before calling us. Retain the text in its
    // owner while the request may outlive that particular Composer instance.
    setPendingDraft(text);
    const canCreate = text.trim() !== '' && ns.loaded && ns.target !== null && !ns.sending;
    const authorization = canCreate ? authorizeRouteAction() : undefined;
    if (authorization === null) return false;

    const result = await ns.send(text);
    if (!current.current.mounted || current.current.lifetime !== lifetime) return false;
    if (result) {
      setPendingDraft('');
      setPendingNavigation({ sessionId: result.sessionId, authorization });
      return true;
    }
    // No project to target → stash the prompt and open the existing handoff.
    if (text.trim() && ns.needsProject && current.current.surfaceActive) openNewProject();
    return false;
  };

  return (
    <>
      <Dialog
        open={open && surfaceActive && !pendingNavigation}
        onOpenChange={(nextOpen) => {
          // Settings suspension closes only the visible modal content. Keep the
          // logical sheet open so its hook lifetime, selections and uncertainty
          // remain intact until the user explicitly closes it.
          if (!nextOpen && open && !surfaceActive) return;
          if (!nextOpen && !ns.sending && !pendingNavigation) close();
        }}
      >
        {surfaceActive && !pendingNavigation && <DialogContent
          className="gap-5"
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => {
            if (!current.current.surfaceActive) event.preventDefault();
          }}
        >
          <DialogTitle className="text-lg font-bold">{t('newSession.title')}</DialogTitle>

          <ProjectPicker
            projects={ns.projects}
            targetId={ns.target?.id}
            onSelect={ns.setSelected}
            onNewProject={openNewProject}
            disabled={ns.sending}
          />
          <div className="flex min-w-0 flex-col gap-2">
            <div className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t('newSession.agent')}</div>
            <AgentRoutePicker
              value={ns.agentRoute}
              agents={ns.agents}
              onChange={ns.setAgentRoute}
              defaultLabel={ns.effectiveDefaultAgentName ? t('newSession.defaultAgentNamed', { name: ns.effectiveDefaultAgentName }) : t('newSession.defaultAgent')}
              disabled={ns.sending}
              align="start"
              triggerClassName="w-full max-w-full"
              modal
              onNavigateAway={close}
            />
          </div>

          {ns.error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
              {ns.error}
              {ns.uncertainSessionId && (
                <Link className="ml-2 underline" to={`/chat/${encodeURIComponent(ns.uncertainSessionId)}`} target="_blank" rel="noopener noreferrer">
                  {t('newSession.inspectSession')}
                </Link>
              )}
            </div>
          )}

          {/* Disabled until projects load successfully, so a failed reload can't
              create a session under a stale/removed cached project. initialDraft
              re-seeds a prompt stashed when the no-project flow closed the sheet. */}
          <Composer
            onSend={send}
            onDraftChange={updateDraft}
            placeholder={t('newSession.placeholder')}
            disabled={ns.sending || !ns.loaded}
            sendDisabled={Boolean(ns.uncertainSessionId)}
            initialDraft={pendingDraft}
          />
        </DialogContent>}
      </Dialog>

      {/* Sibling of the sheet's Dialog (and opened only after the sheet closes)
          so the parent modal's focus trap can't make the folder picker / confirm
          step unreachable on mobile. */}
      {newProjectOpen && (
        <NewProjectDialog
          onClose={() => setNewProjectOpen(false)}
          onCreated={(project) => {
            setNewProjectOpen(false);
            ns.upsertSelectProject(project);
            // Reopen the sheet so the user continues the new-session flow with the
            // freshly created project selected, instead of having to tap ＋ again.
            onOpen();
          }}
        />
      )}
    </>
  );
};
