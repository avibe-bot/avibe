import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Folder, FolderOpen, Loader2, X } from 'lucide-react';

import type { WorkbenchProject } from '../../context/ApiContext';
import { useWorkbenchProjectsActions } from '../../context/WorkbenchProjectsContext';
import { DirectoryBrowser } from '../ui/directory-browser';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { errorMessage } from '@/lib/errorMessage';

interface NewProjectDialogProps {
  onClose: () => void;
  /** The project is already in the shared tree by the time this fires — the
   *  provider owns that commit so it can fence it against an authorization
   *  change landing mid-request. Use it only for what is local to the caller
   *  (close the modal, select the row). It does NOT fire when the commit was
   *  refused, because there is then no row for the caller to point at. */
  onCreated: (project: WorkbenchProject) => void;
}

// Two-phase modal: first the macOS-style DirectoryBrowser picks a folder,
// then a compact confirm card lets the user override the display name and
// fire the create call. The backend defaults display_name to the folder
// basename — keep the input empty to accept that default.
export const NewProjectDialog: React.FC<NewProjectDialogProps> = ({ onClose, onCreated }) => {
  const { t } = useTranslation();
  const { createProject } = useWorkbenchProjectsActions();
  const [phase, setPhase] = useState<'pick' | 'confirm'>('pick');
  const [folderPath, setFolderPath] = useState<string>('');
  const [displayName, setDisplayName] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (phase === 'pick') {
    return (
      <DirectoryBrowser
        onClose={onClose}
        onSelect={(path) => {
          setFolderPath(path);
          setPhase('confirm');
        }}
      />
    );
  }

  const folderBasename = folderPath.split('/').filter(Boolean).pop() || folderPath || '—';

  const submit = async () => {
    if (!folderPath || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const project = await createProject({
        folder_path: folderPath,
        display_name: displayName.trim() || undefined,
      });
      // null = created, but an authorization change landed while we waited, so
      // this document no longer holds a tree to place it in. Just close.
      if (!project) {
        onClose();
        return;
      }
      onCreated(project);
    } catch (err) {
      setError(errorMessage(err) ?? String(err));
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-label={t('workbench.newProjectDialog.title')}
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-md flex-col gap-4 rounded-2xl border border-border-strong bg-surface p-5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <div className="flex size-9 items-center justify-center rounded-lg border border-mint/30 bg-mint/[0.08] text-mint-ink">
            <Folder className="size-4" />
          </div>
          <div className="flex flex-1 flex-col gap-0.5">
            <div className="text-[14px] font-bold text-foreground">{t('workbench.newProjectDialog.title')}</div>
            <div className="text-[11.5px] leading-relaxed text-muted">
              {t('workbench.newProjectDialog.description')}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('workbench.newProjectDialog.cancel')}
            className="text-muted transition hover:text-foreground"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="flex flex-col gap-1.5">
          <label className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted">
            {t('workbench.newProjectDialog.folderLabel')}
          </label>
          <button
            type="button"
            onClick={() => setPhase('pick')}
            className="flex items-center gap-2 rounded-md border border-border-strong bg-surface-2 px-3 py-2 text-left text-[12px] font-mono text-foreground transition hover:bg-foreground/[0.04]"
          >
            <FolderOpen className="size-3.5 shrink-0 text-gold-ink" />
            <span className="flex-1 truncate">{folderPath || '—'}</span>
            <span className="shrink-0 text-[10px] text-muted">{t('workbench.newProjectDialog.pickFolder')}</span>
          </button>
        </div>

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="new-project-display-name"
            className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted"
          >
            {t('workbench.newProjectDialog.displayName')}
          </label>
          <Input
            id="new-project-display-name"
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={folderBasename}
            className="text-[13px]"
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
            }}
            autoFocus
          />
          <div className="text-[10.5px] text-muted">{t('workbench.newProjectDialog.displayNamePlaceholder')}</div>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive-ink">
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={submitting}>
            {t('workbench.newProjectDialog.cancel')}
          </Button>
          <Button
            type="button"
            variant="brand"
            size="sm"
            onClick={submit}
            disabled={!folderPath || submitting}
          >
            {submitting ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                {t('workbench.newProjectDialog.creating')}
              </>
            ) : (
              t('workbench.newProjectDialog.create')
            )}
          </Button>
        </div>
      </div>
    </div>
  );
};
