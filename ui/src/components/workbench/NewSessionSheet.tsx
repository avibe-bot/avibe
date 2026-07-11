import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useNewSession } from '../../lib/useNewSession';
import { useUnsavedChangesActionGuard } from '../../context/useUnsavedChangesActionGuard';
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog';
import { Composer } from './Composer';
import { NewProjectDialog } from './NewProjectDialog';
import { ProjectPicker } from './ProjectPicker';
import { AgentRoutePicker } from './AgentRoutePicker';

interface NewSessionSheetProps {
  open: boolean;
  onClose: () => void;
  onOpen: () => void;
}

// The workbench center ＋ opens this instead of jumping to the home canvas.
// Pick a project (chips, most-recent first), describe the task, and it creates
// the session + routes to /chat with the message pre-seeded — the same flow as
// the desktop Workbench home, surfaced as a mobile bottom sheet (design.pen KSXXB).
// The create flow itself lives in the shared useNewSession hook (one source of
// truth with the home); the sheet only owns its open/close + draft lifecycle.
export const NewSessionSheet: React.FC<NewSessionSheetProps> = ({ open, onClose, onOpen }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const authorizeRouteAction = useUnsavedChangesActionGuard();
  // active: open → the hook reloads + resets per-open (the sheet is permanently
  // mounted by AppShell, so stale submit/error state must not leak across opens).
  const ns = useNewSession({
    active: open,
    loadErrorText: t('newSession.loadError'),
    createFailedText: t('newSession.createFailed'),
  });
  // Stashed prompt: the no-project path closes the sheet (unmounting the
  // Composer) to create a project, so we hold the typed text and re-seed it
  // when the sheet reopens, instead of losing it.
  const [pendingDraft, setPendingDraft] = useState('');
  const [newProjectOpen, setNewProjectOpen] = useState(false);

  // Close the sheet first, THEN open the project dialog: the parent Radix Dialog
  // traps focus/pointer to its own content, so a NewProjectDialog rendered while
  // the sheet is open would be unreachable. Sheet closed → no trap → accessible.
  const openNewProject = () => {
    // Don't tear down the sheet for project creation while a session create is
    // in flight — the pending success would still navigate, stranding the
    // project modal over the new chat.
    if (ns.sending) return;
    onClose();
    setNewProjectOpen(true);
  };

  const send = async (text: string): Promise<boolean> => {
    // Creating the session is irreversible and its target route does not exist until the API
    // succeeds. Confirm before that mutation, then let its one resulting navigation bypass the
    // router blocker so the user sees exactly one prompt.
    const canCreate = text.trim() !== '' && ns.loaded && ns.target !== null && !ns.sending;
    const authorization = canCreate ? authorizeRouteAction() : undefined;
    if (authorization === null) return false;

    const result = await ns.send(text);
    if (result) {
      setPendingDraft('');
      const navigateToSession = () =>
        navigate(`/chat/${encodeURIComponent(result.sessionId)}`, {
          state: { initialMessage: result.initialMessage },
        });
      if (authorization) authorization.runNavigation(navigateToSession);
      else navigateToSession();
      onClose();
      return true;
    }
    // No project to target → stash the prompt and route to the New Project flow.
    const trimmed = text.trim();
    if (trimmed && ns.needsProject) {
      setPendingDraft(trimmed);
      openNewProject();
    }
    return false;
  };

  return (
    <>
      <Dialog open={open} onOpenChange={(o) => { if (!o && !ns.sending) onClose(); }}>
        <DialogContent className="gap-5" onOpenAutoFocus={(e) => e.preventDefault()}>
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
              onNavigateAway={onClose}
            />
          </div>

          {ns.error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
              {ns.error}
            </div>
          )}

          {/* Disabled until projects load successfully, so a failed reload can't
              create a session under a stale/removed cached project. initialDraft
              re-seeds a prompt stashed when the no-project flow closed the sheet. */}
          <Composer
            onSend={send}
            placeholder={t('newSession.placeholder')}
            disabled={ns.sending || !ns.loaded}
            initialDraft={pendingDraft}
          />
        </DialogContent>
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
