import * as React from 'react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';

import { Dialog, DialogContent, DialogDescription, DialogTitle } from './dialog';
import { Button } from './button';
import { MarkdownEditor } from './markdown-editor';

export interface EditorDialogProps {
  open: boolean;
  onClose: () => void;
  /** Heading shown at the top-left of the modal. */
  title: string;
  /** Optional one-line subtitle under the title. */
  description?: string;
  /** Current text; the modal edits a private draft seeded from this on open. */
  value: string;
  /** Commit the edited text. Called on Save. May return a promise — while it is
   *  pending the dialog shows a saving state and stays open; it closes on
   *  resolve and stays open on reject so the in-progress draft isn't lost. */
  onSave: (value: string) => void | Promise<void>;
  placeholder?: string;
  /** Show the Edit/Preview toggle + Markdown preview (default true). */
  markdownPreview?: boolean;
  /** Optional left-aligned footer node derived from the live draft (e.g. a
   *  token/char count). */
  footerHint?: (draft: string) => React.ReactNode;
  /** When false, an outside click or Esc won't close the dialog — only the
   *  explicit Cancel / Save / X. Guards a long edit against an accidental
   *  dismiss. Defaults to true. */
  dismissable?: boolean;
  /** Optional banner rendered above the editor body, e.g. a "loaded from a
   *  fallback file" hint. */
  notice?: React.ReactNode;
  /** Optional full-width row rendered in the footer above the action buttons,
   *  e.g. a related toggle. */
  footerSlot?: React.ReactNode;
}

// Generic full-size text editor in a modal: a large monospace textarea with an
// optional Edit/Preview Markdown toggle, reusing the shared <Markdown> renderer
// and the .vr-markdown styling. Built for agent system prompts first, but kept
// field-agnostic (title / value / onSave) so any long-text field can adopt it
// and grow here later toward a real editor — syntax highlighting, code, docs.
export const EditorDialog: React.FC<EditorDialogProps> = ({
  open,
  onClose,
  title,
  description,
  value,
  onSave,
  placeholder,
  markdownPreview = true,
  footerHint,
  dismissable = true,
  notice,
  footerSlot,
}) => {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  // Reseed the draft each time the modal opens, so a cancelled edit never leaks
  // into the next open and an external change to ``value`` between opens is
  // picked up. The Edit/Preview mode resets on its own — MarkdownEditor remounts
  // with the dialog content on each open.
  useEffect(() => {
    if (open) {
      setDraft(value);
      setSaving(false);
    }
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
  }, [open]);

  const handleSave = () => {
    const result = onSave(draft);
    if (result instanceof Promise) {
      // Async commit (e.g. persisting to a backend): show progress and keep the
      // dialog open on failure so the draft isn't lost.
      setSaving(true);
      result.then(
        () => {
          setSaving(false);
          onClose();
        },
        () => setSaving(false),
      );
    } else {
      onClose();
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Never close mid-save; respect the non-dismissable opt-out.
        if (!next && !saving) onClose();
      }}
    >
      <DialogContent
        className="flex h-[82vh] w-[92vw] max-w-[920px] flex-col gap-0 overflow-hidden p-0"
        keepOnOutsideClick
        onInteractOutside={dismissable ? undefined : (e) => e.preventDefault()}
        onEscapeKeyDown={dismissable ? undefined : (e) => e.preventDefault()}
      >
        {/* Header — title + optional subtitle. pr-12 leaves room for the
            Dialog's built-in close X (absolute, top-right). */}
        <div className="flex flex-col gap-1 border-b border-border px-5 py-4 pr-12">
          <DialogTitle className="text-[15px] font-bold text-foreground">{title}</DialogTitle>
          {description && (
            <DialogDescription className="text-[12px] leading-relaxed text-muted">
              {description}
            </DialogDescription>
          )}
        </div>

        {/* Optional notice banner (e.g. content loaded from a fallback file). */}
        {notice && (
          <div className="border-b border-border bg-surface-2 px-5 py-2 text-[12px] leading-relaxed text-muted">
            {notice}
          </div>
        )}

        {/* Body — the shared Markdown editing surface fills the remaining height. */}
        <MarkdownEditor
          value={draft}
          onChange={setDraft}
          placeholder={placeholder}
          markdownPreview={markdownPreview}
          onSubmit={handleSave}
        />

        {/* Footer — optional slot (e.g. a toggle) above the draft hint + actions. */}
        <div className="flex flex-col gap-3 border-t border-border px-5 py-3">
          {footerSlot && <div>{footerSlot}</div>}
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-muted">{footerHint?.(draft)}</span>
            <div className="flex items-center gap-2">
              <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={saving}>
                {t('common.cancel')}
              </Button>
              <Button type="button" variant="brand" size="sm" onClick={handleSave} disabled={saving}>
                {saving && <Loader2 className="size-3.5 animate-spin" />}
                {t('common.save')}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};
