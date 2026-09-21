// The fields that ask for an API key, in the one order they are asked in.
//
// Promoted out of `AddApiKeyDialog` for the reason `dialogFields.tsx` was before
// it: setup asks for the same credential inside a different frame, and two
// dialogs asking the same question must be the same question — same vendor
// picker, same endpoint default, same protocol rule, same reveal control. A
// second local copy is how they drift by a hint and a field order.
//
// It renders a fragment rather than a container: what holds these fields is the
// host's frame, and the Model Hub dialog and the setup dialog do not agree on
// that. State is the host's too — it is what setup preserves when a tab is
// switched away from and back.
import * as React from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Combobox, type ComboboxOption } from '@/components/ui/combobox';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

import { PROTOCOL_COPY_KEYS } from './addApiKeyState';
import { API_KEY_VENDOR_PRESETS, apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import {
  draftNameValid,
  draftProtocol,
  selectVendor,
  type ApiKeySourceDraft,
} from './apiKeySourceDraft';
import { Field } from './dialogFields';
import { ProtocolGlyph } from './protocolGlyph';
import { SOURCE_PROTOCOLS, type SourceProtocol } from './types';
import { VendorGlyph } from './vendorGlyph';

// The form's own dressing travels with the form. Every class below is declared
// in this stylesheet, and its only other importer is the Model Hub page — so a
// host outside that page (setup) would otherwise render these fields naked and
// discover it visually. Vite dedupes the second import; the page keeps its own
// because it draws far more of this sheet than the form does.
import './modelHubSurface.css';

const ProtocolSegments: React.FC<{
  id?: string;
  disabled: boolean;
  selection: SourceProtocol;
  onSelect: (value: SourceProtocol) => void;
}> = ({ id, disabled, selection, onSelect }) => {
  const { t } = useTranslation();
  return (
    <div
      id={id}
      role="group"
      aria-label={t('settings.models.addKey.field.protocol')}
      className="model-hub-add-key-segments flex max-w-full flex-wrap"
    >
      {SOURCE_PROTOCOLS.map((item) => (
        <button
          key={item}
          type="button"
          disabled={disabled}
          aria-pressed={selection === item}
          className={cn(
            'model-hub-add-key-segment',
            selection === item && 'is-selected',
          )}
          onClick={() => onSelect(item)}
        >
          <ProtocolGlyph protocol={item} />
          {t(PROTOCOL_COPY_KEYS[item])}
        </button>
      ))}
    </div>
  );
};

/**
 * The 服务商 field's rows: the shipped catalog in the order the file ships, then
 * the one entry that is not a vendor at all, each carrying its mark.
 *
 * File order because that order is a curated ranking, not an accident of how the
 * rows were appended: the vendors most users are here to add sit at the top, and
 * a name is only what you scan for once the list is long enough to have lost you.
 * Re-sorting here would put the ranking in a second place and make the catalog's
 * own order unobservable — so this reads the file verbatim, and moving a vendor
 * up the list is an edit to `vibe/data/api_key_vendors.json` and to nothing else.
 * 自定义 ranks nowhere: it is the absence of a vendor, so it sits after all of
 * them rather than inside them, while staying the value the field opens on.
 *
 * The mark is why the field is no longer a `<select>`. A vendor is recognised by
 * its logo long before its name is read, and an `<option>` holds text only — so
 * the closed control could only ever show what an option could hold, dropping
 * the mark exactly where the choice has already been made. What replaces it is
 * this app's one picker, `Combobox`, given a mark per row; a second local
 * implementation of the same trigger, panel, and keyboard would only be a place
 * for the two to diverge.
 */
const useVendorOptions = (): ComboboxOption[] => {
  const { t } = useTranslation();
  return React.useMemo(() => ([
    ...API_KEY_VENDOR_PRESETS.map((preset) => ({ value: preset.id, label: preset.label })),
    { value: CUSTOM_VENDOR, label: t('settings.models.addKey.field.vendor.custom') },
  ].map((option) => ({ ...option, icon: <VendorGlyph vendor={option.value} /> }))), [t]);
};

/**
 * The credential field, with its reveal control.
 *
 * Exported because key replacement asks for exactly this one field and nothing
 * else around it: the same input, the same masking, the same 显示/隐藏 label.
 */
export const ApiKeyField: React.FC<{
  value: string;
  revealed: boolean;
  disabled: boolean;
  label: React.ReactNode;
  autoFocus?: boolean;
  onChange: (value: string) => void;
  onToggleReveal: () => void;
  onEnter?: () => void;
}> = ({ value, revealed, disabled, label, autoFocus, onChange, onToggleReveal, onEnter }) => {
  const { t } = useTranslation();
  return (
    <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={label}>
      {(id) => (
        <span className="model-hub-add-key-secret relative flex items-center">
          <Input
            id={id}
            value={value}
            type={revealed ? 'text' : 'password'}
            disabled={disabled}
            autoFocus={autoFocus}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || disabled || !onEnter) return;
              event.preventDefault();
              onEnter();
            }}
            className="model-hub-add-key-input w-full pr-10 font-mono"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="model-hub-ink-59 absolute right-1 size-7"
            aria-label={t(`settings.models.addKey.field.apiKey.${revealed ? 'conceal' : 'reveal'}`)}
            disabled={disabled}
            onClick={onToggleReveal}
          >
            {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
          </Button>
        </span>
      )}
    </Field>
  );
};

/**
 * @param onChange receives the whole next draft and the field that moved. The
 *   field is there because hosts react differently to different edits — the Model
 *   Hub dialog clears a save failure on any edit but resets its phase outright on
 *   a vendor change — and inferring which one changed by diffing would be the
 *   same rule written twice.
 * @param onSubmit Enter in the key field, when the host has somewhere to send it.
 * @param keyLabel Names the credential field. Setup titles it by the chosen brand
 *   — 「OpenAI API Key」 — because it arrives there having just been asked for that
 *   brand by name; the generic label is what a form opened from nowhere says.
 */
export const ApiKeySourceForm: React.FC<{
  draft: ApiKeySourceDraft;
  disabled: boolean;
  revealed: boolean;
  autoFocusKey?: boolean;
  keyLabel?: React.ReactNode;
  onChange: (next: ApiKeySourceDraft, field: keyof ApiKeySourceDraft) => void;
  onToggleReveal: () => void;
  onSubmit?: () => void;
}> = ({ draft, disabled, revealed, autoFocusKey, keyLabel, onChange, onToggleReveal, onSubmit }) => {
  const { t } = useTranslation();
  const vendorOptions = useVendorOptions();
  const vendorPreset = apiKeyVendorPreset(draft.vendor);
  const nameValid = draftNameValid(draft);

  return (
    <>
      {/* First, because it is the field the rest are conditioned on: it
          decides what the Base URL starts as and whether the interface is
          still a question. */}
      <Field
        className="model-hub-add-key-field"
        labelClassName="model-hub-add-key-label"
        hintClassName="model-hub-add-key-hint"
        label={t('settings.models.addKey.field.vendor')}
        hint={t('settings.models.addKey.field.vendor.hint')}
      >
        {(id) => (
          <Combobox
            id={id}
            // The label points here, but a `for` association contributes
            // nothing to a button's accessible name, so the field has to
            // name itself. The primitive appends the selection to what is
            // passed here: the label alone would replace the trigger's
            // contents, and those contents are the chosen vendor — the one
            // thing a picker exists to report.
            ariaLabel={t('settings.models.addKey.field.vendor')}
            className="model-hub-add-key-input"
            options={vendorOptions}
            value={draft.vendor}
            onValueChange={(value) => onChange(selectVendor(draft, value), 'vendor')}
            // A vendor is a catalog row, and the request sends its id: there
            // is no typed value this field could accept.
            allowCustomValue={false}
            disabled={disabled}
            searchPlaceholder={t('settings.models.addKey.field.vendor.search')}
            emptyText={t('settings.models.addKey.field.vendor.empty')}
          />
        )}
      </Field>
      <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={t('settings.models.addKey.field.name')}>
        {(id) => <Input id={id} value={draft.displayName} disabled={disabled} aria-invalid={!nameValid} onChange={(event) => onChange({ ...draft, displayName: event.target.value }, 'displayName')} className="model-hub-add-key-input" />}
      </Field>
      <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" hintClassName="model-hub-add-key-hint" label={t('settings.models.addKey.field.baseUrl')} hint={t('settings.models.addKey.field.baseUrl.hint')}>
        {(id) => <Input id={id} value={draft.baseUrl} disabled={disabled} autoComplete="url" spellCheck={false} onChange={(event) => onChange({ ...draft, baseUrl: event.target.value }, 'baseUrl')} className="model-hub-add-key-input font-mono" />}
      </Field>
      <ApiKeyField
        value={draft.apiKey}
        revealed={revealed}
        disabled={disabled}
        autoFocus={autoFocusKey}
        label={keyLabel ?? t('settings.models.addKey.field.apiKey')}
        onChange={(value) => onChange({ ...draft, apiKey: value }, 'apiKey')}
        onToggleReveal={onToggleReveal}
        onEnter={onSubmit}
      />

      <div className="model-hub-add-key-protocol-area">
        <span className="model-hub-add-key-label">{t('settings.models.addKey.field.protocol')}</span>
        {vendorPreset ? (
          <div className="model-hub-add-key-protocol-idle-row">
            <span className="model-hub-add-key-protocol-active">
              <ProtocolGlyph protocol={draftProtocol(draft)} />{t(PROTOCOL_COPY_KEYS[draftProtocol(draft)])}
            </span>
          </div>
        ) : (
          <ProtocolSegments
            disabled={disabled}
            selection={draft.protocol}
            onSelect={(value) => onChange({ ...draft, protocol: value }, 'protocol')}
          />
        )}
        <p className="model-hub-add-key-hint">{t('settings.models.addKey.protocol.saveFirstHint')}</p>
      </div>
    </>
  );
};
