import * as React from 'react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bot, Copy, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { GlobalPromptFile } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog';
import { Button } from '../ui/button';
import { MarkdownEditor } from '../ui/markdown-editor';
import { BACKEND_ORDER, BACKEND_LABEL, BACKEND_TEXT, BACKEND_DOT, type Backend } from '../../lib/backendAccent';

type FileMeta = { path: string; filename: string; exists: boolean; readError: boolean };

const emptyStringMap = (): Record<Backend, string> => ({ claude: '', opencode: '', codex: '' });
const emptyMetaMap = (): Record<Backend, FileMeta | null> => ({ claude: null, opencode: null, codex: null });

// Index an API response (one entry per backend) into a per-backend map over the
// canonical BACKEND_ORDER, so missing/extra ids never desync the editor state.
const mapContent = (files: GlobalPromptFile[]): Record<Backend, string> => {
  const out = emptyStringMap();
  for (const backend of BACKEND_ORDER) out[backend] = files.find((f) => f.backend === backend)?.content ?? '';
  return out;
};
const mapMeta = (files: GlobalPromptFile[]): Record<Backend, FileMeta | null> => {
  const out = emptyMetaMap();
  for (const backend of BACKEND_ORDER) {
    const file = files.find((f) => f.backend === backend);
    out[backend] = file
      ? { path: file.path, filename: file.filename, exists: file.exists, readError: file.read_error }
      : null;
  }
  return out;
};

// Edit each backend's *global* instructions file from one place — the global
// twin of the per-project AGENTS.md editor. Backends are tabs; each carries its
// own in-memory draft seeded from the loaded file, so edits across tabs survive
// switching until the user Saves (one backend) or Syncs (overwrites all three
// with the active draft). Reuses the shared MarkdownEditor surface.
export const GlobalPromptsDialog: React.FC<{ open: boolean; onClose: () => void }> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [loading, setLoading] = useState(true);
  const [saved, setSaved] = useState<Record<Backend, string>>(emptyStringMap);
  const [drafts, setDrafts] = useState<Record<Backend, string>>(emptyStringMap);
  const [meta, setMeta] = useState<Record<Backend, FileMeta | null>>(emptyMetaMap);
  const [active, setActive] = useState<Backend>('claude');
  const [busy, setBusy] = useState(false);

  // Load every backend's file once per open and seed both the saved baseline
  // and the editable drafts from it.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setActive('claude');
    api.getGlobalPrompts().then(
      (res) => {
        if (cancelled) return;
        setSaved(mapContent(res.backends));
        setDrafts(mapContent(res.backends));
        setMeta(mapMeta(res.backends));
        setLoading(false);
      },
      () => {
        // The shared apiFetch layer already surfaced the error toast.
        if (!cancelled) onClose();
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const isDirty = (backend: Backend) => drafts[backend] !== saved[backend];
  const anyDirty = BACKEND_ORDER.some(isDirty);
  // Sync overwrites every backend, so any unreadable target (not just the
  // active one) must block it — otherwise sync would clobber a file whose
  // contents were never shown, defeating the read-error safeguard.
  const anyReadError = BACKEND_ORDER.some((backend) => meta[backend]?.readError);
  const activeMeta = meta[active];

  // Fold the refreshed file list back into the saved baseline + metadata
  // (clearing the active tab's dirty state) without disturbing drafts the user
  // is still editing on other tabs.
  const applyResult = (files: GlobalPromptFile[]) => {
    setSaved(mapContent(files));
    setMeta(mapMeta(files));
  };

  const handleClose = () => {
    if (busy) return;
    if (anyDirty && !window.confirm(t('globalPrompts.discardConfirm'))) return;
    onClose();
  };

  const handleSave = async () => {
    if (busy || !isDirty(active) || meta[active]?.readError) return;
    setBusy(true);
    try {
      const res = await api.saveGlobalPrompts({ content: drafts[active], backends: [active] });
      applyResult(res.backends);
      showToast(t('globalPrompts.saved', { backend: BACKEND_LABEL[active] }), 'success');
    } catch {
      // apiFetch already toasted; keep the dialog + draft so nothing is lost.
    } finally {
      setBusy(false);
    }
  };

  const handleSync = async () => {
    if (busy || anyReadError) return;
    if (!window.confirm(t('globalPrompts.syncConfirm', { backend: BACKEND_LABEL[active] }))) return;
    setBusy(true);
    try {
      const res = await api.saveGlobalPrompts({ content: drafts[active], backends: [...BACKEND_ORDER] });
      applyResult(res.backends);
      // Every backend now holds the active content — reflect it across all tabs
      // so they read clean and show the synced text.
      setDrafts(mapContent(res.backends));
      showToast(t('globalPrompts.synced'), 'success');
    } catch {
      // apiFetch already toasted.
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) handleClose();
      }}
    >
      <DialogContent
        className="flex h-[82vh] w-[92vw] max-w-[920px] flex-col gap-0 overflow-hidden p-0"
        // Non-dismissable: an outside click / Esc would discard multi-tab edits,
        // so closing goes through the X / Cancel (which confirm when dirty).
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        {/* Header — pr-12 leaves room for the Dialog's built-in close X. */}
        <div className="flex flex-col gap-1 border-b border-border px-5 py-4 pr-12">
          <DialogTitle className="text-[15px] font-bold text-foreground">{t('globalPrompts.title')}</DialogTitle>
          <DialogDescription className="text-[12px] leading-relaxed text-muted">
            {t('globalPrompts.description')}
          </DialogDescription>
        </div>

        {!loading && (
          <>
            {/* Backend tabs — accent-colored, with a dot marking unsaved edits. */}
            <div role="tablist" className="flex items-center gap-1 border-b border-border px-3">
              {BACKEND_ORDER.map((backend) => {
                const activeTab = backend === active;
                return (
                  <Button
                    key={backend}
                    type="button"
                    role="tab"
                    aria-selected={activeTab}
                    variant="ghost"
                    size="sm"
                    onClick={() => setActive(backend)}
                    className={clsx(
                      'h-auto gap-2 rounded-none border-b-2 px-3 py-2.5 hover:bg-transparent',
                      activeTab
                        ? clsx('border-current', BACKEND_TEXT[backend])
                        : 'border-transparent text-muted hover:text-foreground',
                    )}
                  >
                    <Bot className={clsx('size-3.5', activeTab ? BACKEND_TEXT[backend] : 'text-muted')} />
                    {BACKEND_LABEL[backend]}
                    {isDirty(backend) && (
                      <span className={clsx('size-1.5 rounded-full', BACKEND_DOT[backend])} aria-hidden />
                    )}
                  </Button>
                );
              })}
            </div>

            {/* Tip — the exact file being edited + what it does, in plain terms. */}
            {activeMeta && (
              <div className="flex flex-col gap-0.5 border-b border-border bg-surface-2 px-5 py-2">
                <span className="font-mono text-[11px] text-foreground">{activeMeta.path}</span>
                {activeMeta.readError ? (
                  <span className="text-[11px] leading-relaxed text-gold-ink">{t('globalPrompts.readError')}</span>
                ) : (
                  <span className="text-[11px] leading-relaxed text-muted">
                    {t('globalPrompts.tipEffect', { backend: BACKEND_LABEL[active] })}
                    {!activeMeta.exists && <> · {t('globalPrompts.missingHint')}</>}
                  </span>
                )}
              </div>
            )}
          </>
        )}

        {/* Body — loading spinner, then the shared Markdown editing surface. */}
        {loading ? (
          <div className="flex min-h-0 flex-1 items-center justify-center">
            <Loader2 className="size-5 animate-spin text-muted" />
          </div>
        ) : (
          <MarkdownEditor
            value={drafts[active]}
            onChange={(next) => setDrafts((prev) => ({ ...prev, [active]: next }))}
            placeholder={t('globalPrompts.placeholder')}
            onSubmit={handleSave}
          />
        )}

        {/* Footer — Sync (left) + Cancel / Save (right). */}
        <div className="flex items-center justify-between gap-3 border-t border-border px-5 py-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleSync}
            disabled={busy || loading || anyReadError}
          >
            <Copy className="size-3.5" />
            {t('globalPrompts.sync')}
          </Button>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" size="sm" onClick={handleClose} disabled={busy}>
              {t('common.cancel')}
            </Button>
            <Button
              type="button"
              variant="brand"
              size="sm"
              onClick={handleSave}
              disabled={busy || loading || !!activeMeta?.readError || !isDirty(active)}
            >
              {busy && <Loader2 className="size-3.5 animate-spin" />}
              {t('globalPrompts.save')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};
