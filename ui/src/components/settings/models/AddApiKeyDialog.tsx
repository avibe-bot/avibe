// 添加 API Key dialog (frame 06r): the only form dialog for adding a source.
// Vendor picker prefills the official base URL (editable for compatible /
// relay endpoints); the primary button is test-and-add — it validates the key,
// discovers models, and reports the count before the dialog dismisses.
import * as React from 'react';
import { CheckCircle2, Globe, KeyRound, Plus, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { modelsApi, type Adoption } from './modelsApi';
import { AdoptionNote } from './AdoptionNote';
import { Field } from './dialogFields';
import { adoptionVerdict } from './sufficiency';
import { DEFAULT_VENDOR, VENDOR_OPTIONS } from './vendorMeta';
import type { Source } from './types';
import { errorMessage } from '@/lib/errorMessage';

/** Nothing created yet. `skipped_by: null` rather than `[]` for the reason the
 *  reader defaults it that way: 「nobody was left out」 is a claim, and no
 *  creation has made it. */
const NO_ADOPTION: Adoption = { adopted_by: [], skipped_by: null };

type Phase = 'edit' | 'submitting' | 'done' | 'error';

export const AddApiKeyDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  onAdded: (source: Source) => void;
}> = ({ open, onClose, onAdded }) => {
  const { t } = useTranslation();
  const [vendor, setVendor] = React.useState(DEFAULT_VENDOR.value);
  const [apiKey, setApiKey] = React.useState('');
  const [baseUrl, setBaseUrl] = React.useState('');
  const [phase, setPhase] = React.useState<Phase>('edit');
  const [discovered, setDiscovered] = React.useState(0);
  // One state for both halves of the tail: the note and the auto-close timer each
  // read both, and two states could hold one half of an older response.
  const [adoption, setAdoption] = React.useState<Adoption>(NO_ADOPTION);
  const [error, setError] = React.useState<string | null>(null);
  const closeTimer = React.useRef<number | null>(null);
  // Bumped on every open/close so a test-and-add resolving after the dialog was
  // closed or reopened is dropped instead of clobbering the new state.
  const submitSeq = React.useRef(0);

  // Reset the form each time the dialog opens; clear any pending auto-close.
  React.useEffect(() => {
    submitSeq.current += 1;
    if (open) {
      setVendor(DEFAULT_VENDOR.value);
      setApiKey('');
      setBaseUrl(DEFAULT_VENDOR.base_url ?? '');
      setPhase('edit');
      setDiscovered(0);
      setAdoption(NO_ADOPTION);
      setError(null);
    }
    return () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    };
  }, [open]);

  const onVendorChange = (value: string) => {
    setVendor(value);
    const meta = VENDOR_OPTIONS.find((v) => v.value === value);
    // Official vendors prefill their base URL (editable); 自定义 clears it.
    setBaseUrl(meta?.base_url ?? '');
  };

  const submit = async () => {
    if (!apiKey.trim()) return;
    const seq = submitSeq.current;
    setPhase('submitting');
    setError(null);
    try {
      const { source, adopted_by, skipped_by } = await modelsApi.createApiKeySource({
        kind: 'api_key',
        vendor,
        base_url: baseUrl.trim() || null,
        key: apiKey.trim(),
      });
      if (submitSeq.current !== seq) return; // dialog closed/reopened mid-request
      setDiscovered(source.models.length);
      setAdoption({ adopted_by, skipped_by });
      setPhase('done');
      onAdded(source);
      // Auto-dismiss only when the note is pure confirmation. 「还没有 Agent
      // 启用它」 is an instruction, and 1.5s is not long enough to read one — a
      // dialog that closes itself over that sentence is how the user ends up
      // believing a working key is in service.
      //
      // `covered` and nothing weaker: a non-empty adopter list does not rule out a
      // `custom` backend that skipped the key, and that sentence is an instruction
      // too. `skipped_by` is what makes `covered` reachable at all — a server that
      // omits it leaves the verdict `indeterminate`, and the dialog waits, which is
      // the same answer this site gave before the field existed.
      if (adoptionVerdict(adopted_by, skipped_by).kind === 'covered')
        closeTimer.current = window.setTimeout(onClose, 1500);
    } catch (e) {
      if (submitSeq.current !== seq) return;
      // The engine reports a machine-readable `code`; fall back to the message
      // and then to a generic key so the copy lookup always has something.
      const rawCode = (e as { code?: unknown } | null | undefined)?.code;
      const code = (typeof rawCode === 'string' ? rawCode : undefined) || errorMessage(e) || 'discovery_failed';
      setError(code === 'engine_down' ? (t('settings.models.errors.engineDown') as string) : code);
      setPhase('error');
    }
  };

  return (
    // Block close (Esc / overlay / X) while the test-and-add request is in
    // flight — the source is provisioned server-side, so closing mid-request
    // would leave a created source the UI never reflected.
    <Dialog open={open} onOpenChange={(v) => !v && phase !== 'submitting' && onClose()}>
      <DialogContent className="max-w-[560px] gap-5">
        <DialogHeader>
          <DialogTitle className="text-[18px] font-bold">{t('settings.models.addKey.title')}</DialogTitle>
          <DialogDescription>{t('settings.models.addKey.subtitle')}</DialogDescription>
        </DialogHeader>

        <Field label={t('settings.models.addKey.vendorLabel')} hint={t('settings.models.addKey.vendorHint')}>
          {(id) => (
            <Select
              id={id}
              value={vendor}
              onChange={(e) => onVendorChange(e.target.value)}
              className="h-11 text-[14px]"
            >
              {VENDOR_OPTIONS.map((v) => (
                <option key={v.value} value={v.value}>
                  {t(v.labelKey)}
                </option>
              ))}
            </Select>
          )}
        </Field>

        <Field label={t('settings.models.addKey.keyLabel')} mono icon={KeyRound}>
          {(id) => (
            <Input
              id={id}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('settings.models.field.apiKeyPlaceholder')}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="h-11 pl-9 font-mono text-[14px]"
              disabled={phase === 'submitting' || phase === 'done'}
            />
          )}
        </Field>

        <Field
          label={t('settings.models.addKey.baseUrlLabel')}
          mono
          icon={Globe}
          hint={t('settings.models.addKey.baseUrlHint')}
        >
          {(id) => (
            <Input
              id={id}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('settings.models.field.baseUrlPlaceholder')}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className="h-11 pl-9 font-mono text-[14px]"
              disabled={phase === 'submitting' || phase === 'done'}
            />
          )}
        </Field>

        {phase === 'done' && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
              <CheckCircle2 className="size-4 shrink-0" />
              <span>{t('settings.models.addKey.discovered', { count: discovered })}</span>
            </div>
            <AdoptionNote adoptedBy={adoption.adopted_by} skippedBy={adoption.skipped_by} />
          </div>
        )}
        {phase === 'error' && (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>{t('settings.models.addKey.failed', { detail: error })}</span>
          </div>
        )}

        <DialogFooter>
          <div className="flex items-center gap-2">
            {/* 关闭, not 取消, once the source exists: after a successful add this
                is the way out of a dialog that no longer auto-dismisses, and
                「取消」 on a committed credential reads as an undo. */}
            <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={onClose} disabled={phase === 'submitting'}>
              {t(phase === 'done' ? 'common.close' : 'common.cancel')}
            </Button>
            <Button
              variant="brand"
              size="sm"
              className="h-10 sm:h-9"
              onClick={() => void submit()}
              disabled={phase === 'submitting' || phase === 'done' || !apiKey.trim()}
            >
              <Plus className="size-4" />
              {phase === 'submitting' ? t('settings.models.addKey.testing') : t('settings.models.addSource')}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
