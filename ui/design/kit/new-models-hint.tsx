// Proposal: Vibey's model menu offers models the user's own Sources newly list, as one quiet row at the
// foot of the model list, and Settings › 模型 › add puts the same few first. Nothing outside the menu
// marks them: no badge, no dot, no notification.
//
// The menu is drawn here, not with the shipped AgentRoutePicker, because the row lives inside it and the
// picker mixes its reads with its markup (ui/design/AGENTS.md). The menu's parts copy the compact
// picker's markup (RouteColumn, RouteItem, PickerExitButton, EffortChip) and sit in the shipped
// ResponsiveMenu, so the popover and the phone sheet are the product's own. When this ships, the frames
// switch to the shipped picker and this file is deleted.
import { Check, ChevronDown, ChevronRight, Plus, Search, X } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { BackendAvatar } from '@/components/visual';
import { useDraft } from './draft';

/** A model as the menu names it: the label from the catalog, the ID under it. */
export interface DraftModel {
  id: string;
  label: string;
  efforts: string[];
}

/** The hint row's state. `many` with `open` lists the names; one model is named in the row itself. */
export type NewModelsHint =
  | { kind: 'none' }
  | { kind: 'one'; model: DraftModel }
  | { kind: 'many'; models: DraftModel[]; open: boolean };

const noop = () => {};

// ---- The menu's parts, as the compact AgentRoutePicker writes them ----

const RouteColumn = ({ title, className, children }: { title: string; className?: string; children: React.ReactNode }) => (
  <div className={clsx('flex min-w-0 flex-col overflow-y-auto p-1.5 sm:max-h-[320px]', className)}>
    <div className="px-2 pb-1 pt-0.5 text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{title}</div>
    {children}
  </div>
);

const RouteItem = ({ active, children }: { active: boolean; children: React.ReactNode }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    className={clsx(
      'h-auto w-full justify-start gap-2 rounded px-2 py-1.5 text-left text-[12px] font-normal',
      active ? 'bg-cyan/[0.10] text-cyan-ink hover:bg-cyan/[0.10] hover:text-cyan-ink' : 'text-foreground hover:bg-wash',
    )}
  >
    {children}
  </Button>
);

const ModelLines = ({ model }: { model: DraftModel }) => (
  <span className="flex min-w-0 flex-1 flex-col">
    <span className="whitespace-normal break-words font-semibold leading-snug">{model.label}</span>
    <span className="whitespace-normal break-words font-mono text-[10px] leading-snug text-muted">{model.id}</span>
  </span>
);

const PickerExitButton = ({ label }: { label: string }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    className="mt-1 h-auto w-full justify-start gap-1.5 rounded px-2 py-1.5 text-[11px] font-medium text-cyan-ink hover:bg-cyan/[0.08] hover:text-cyan-ink"
  >
    <Plus className="size-3.5" />
    {label}
  </Button>
);

const EffortChip = ({ active, label }: { active: boolean; label: string }) => (
  <button
    type="button"
    role="radio"
    aria-checked={active}
    className={clsx(
      'min-w-[3rem] flex-1 whitespace-nowrap rounded-[4px] border px-2 py-1.5 text-[12px] capitalize transition-colors',
      active ? 'border-cyan/40 bg-cyan-soft font-bold text-cyan-ink' : 'border-transparent font-medium text-muted hover:text-foreground',
    )}
  >
    {label}
  </button>
);

// ---- The proposal: the hint row ----

/** 「添加」 at a new model's row end: a label in the cyan of 「+ 添加模型」, not a button of its own; the
 *  whole row is the target. It puts the model in the menu and picks it, as the menu's 添加模型 does. */
const AddLabel = () => {
  const d = useDraft();
  return <span data-hint-add="" className="shrink-0 text-[11px] font-medium text-cyan-ink">{d('添加', 'Add')}</span>;
};

/** A new model's row, tapped anywhere to add it: shaped like the model rows above, 40px tall on a phone. */
const NewModelButton = ({ children }: { children: React.ReactNode }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    className="h-auto min-w-0 flex-1 justify-start gap-2 rounded px-2 py-1.5 text-left text-[12px] font-normal text-foreground hover:bg-cyan/[0.08] max-md:min-h-10"
  >
    {children}
  </Button>
);

/** The close control: one tap and the row never comes back for these models. Hover or focus names it.
 *  On a phone its target is 40px and it keeps 12px from the row it closes, so a missed 「添加」 does not
 *  end the hint for good. */
const CloseControl = () => {
  const d = useDraft();
  const label = d('不再提示', "Don't show again");
  return (
    <span className="group/hint-close relative shrink-0 max-md:-mr-2">
      <Button
        type="button"
        variant="ghost"
        size="2xs"
        aria-label={label}
        data-hint-close=""
        className="w-6 px-0 text-muted hover:text-foreground max-md:size-10"
      >
        <X />
      </Button>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full right-0 z-50 mb-1.5 whitespace-nowrap rounded-md bg-foreground px-2.5 py-1.5 text-[11px] leading-snug text-background opacity-0 shadow-elevation-2 transition-opacity group-hover/hint-close:opacity-100 group-has-[:focus-visible]/hint-close:opacity-100"
      >
        {label}
      </span>
    </span>
  );
};

/** The count line: 「有 2 个新模型可以添加」 with its chevron and ×. */
const CountLine = ({ count, open }: { count: number; open: boolean }) => {
  const d = useDraft();
  return (
    <div className="flex items-center gap-1 pr-0.5 max-md:gap-3">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-expanded={open}
        className="h-auto min-w-0 flex-1 justify-start gap-1 rounded px-2 py-1.5 text-left text-[11px] font-medium text-muted hover:bg-wash hover:text-foreground max-md:min-h-10"
      >
        <span className="truncate">{d(`有 ${count} 个新模型可以添加`, `${count} new models you can add`)}</span>
        <ChevronDown aria-hidden className={clsx('size-3 shrink-0 transition-transform', open && 'rotate-180')} />
      </Button>
      <CloseControl />
    </div>
  );
};

/** Closed, the hint is one line in the model list's pinned foot, above 添加模型: one model named with
 *  「添加」, or a count that opens. Pinned, so every opened menu shows it, however long the list. */
export function NewModelsLine({ hint }: { hint: NewModelsHint }) {
  const d = useDraft();
  if (hint.kind === 'none' || (hint.kind === 'many' && hint.open)) return null;
  if (hint.kind === 'one') {
    return (
      <div data-new-models="" className="mt-1 flex items-center gap-1 pr-0.5 max-md:gap-3">
        <NewModelButton>
          <span className="flex min-w-0 flex-1 items-baseline gap-1.5">
            <span className="shrink-0 text-[11px] font-medium text-muted">{d('新模型', 'New model')}</span>
            <span className="truncate font-semibold" title={hint.model.id}>{hint.model.label}</span>
          </span>
          <AddLabel />
        </NewModelButton>
        <CloseControl />
      </div>
    );
  }
  return <div data-new-models="" className="mt-1"><CountLine count={hint.models.length} open={false} /></div>;
}

/** Opened, the hint is one block that reads top to bottom, the count line and then the names under it,
 *  at the end of the list. It scrolls with the list, so the names never crowd the models out of a
 *  phone's 40vh column; opening brings it into view. Dividers above (its own) and below (the foot's). */
export function NewModelsOpened({ hint }: { hint: NewModelsHint }) {
  if (hint.kind !== 'many' || !hint.open) return null;
  return (
    <div data-new-models="" data-new-models-open="" className="-mx-1.5 mt-1 flex flex-col border-t border-border px-1.5 py-1">
      <CountLine count={hint.models.length} open />
      {hint.models.map((model) => (
        <NewModelButton key={model.id}>
          <ModelLines model={model} />
          <AddLabel />
        </NewModelButton>
      ))}
    </div>
  );
}

// ---- The menu around it ----

export interface ModelMenuDraftProps {
  models: DraftModel[];
  current: string;
  effort: string;
  hint: NewModelsHint;
}

const effortOrder = ['low', 'medium', 'high', 'xhigh', 'max'];

function MenuBody({ models, current, effort, hint }: ModelMenuDraftProps) {
  const { t } = useTranslation();
  const efforts = models.find((model) => model.id === current)?.efforts ?? [];
  return (
    <div className="flex flex-col">
      <button type="button" className="flex items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-wash">
        <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('chat.picker.agent')}</span>
        <span className="ml-auto flex min-w-0 items-center gap-1.5">
          <BackendAvatar backend="vibey" className="size-3.5 shrink-0" />
          <span className="truncate text-[12px] font-semibold text-foreground">vibey</span>
          <ChevronRight className="size-3.5 shrink-0 text-muted" />
        </span>
      </button>
      <RouteColumn title={t('chat.picker.model')} className="max-h-[40vh] border-t border-border">
        {models.map((model) => (
          <RouteItem key={model.id} active={model.id === current}>
            <ModelLines model={model} />
            {model.id === current && <Check className="size-3.5 shrink-0" />}
          </RouteItem>
        ))}
        <NewModelsOpened hint={hint} />
        <div className="sticky -bottom-1.5 z-10 -mx-1.5 -mb-1.5 border-t border-border bg-panel px-1.5 pb-1.5">
          <NewModelsLine hint={hint} />
          <PickerExitButton label={t('chat.picker.addModel')} />
        </div>
      </RouteColumn>
      <div className="border-t border-border p-3">
        <div className="flex items-center pb-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('chat.picker.effort')}</span>
          {/* The Fast toggle as the picker draws it where the route offers no fast tier. */}
          <span className="ml-auto">
            <button
              type="button"
              aria-pressed={false}
              disabled
              className="inline-flex items-center gap-1 rounded-full border border-edge bg-field px-2 py-0.5 text-[11px] font-semibold text-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden className="size-[11px] shrink-0"><path d="M13.6 2.2 6.4 13.1h5l-1.5 8.7 7.7-11.4h-5.2z" /></svg>
              {t('chat.picker.fast')}
            </button>
          </span>
        </div>
        <div role="radiogroup" aria-label={t('chat.picker.effort')} className="flex flex-wrap gap-0.5 rounded-md border border-edge bg-field p-0.5">
          <EffortChip active={false} label={t('chat.picker.effortDefault')} />
          {effortOrder.filter((option) => efforts.includes(option)).map((option) => (
            <EffortChip key={option} active={option === effort} label={t(`chat.picker.effortOptions.${option}`, { defaultValue: option })} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** The Session's composer with its model menu open: a popover on desktop, the bottom sheet on a phone. */
export function ModelMenuDraft(props: ModelMenuDraftProps) {
  const { t } = useTranslation();
  const currentLabel = props.models.find((model) => model.id === props.current)?.label ?? props.current;
  const trigger = (
    <Button
      type="button"
      variant="outline"
      className="inline-flex h-9 min-w-0 items-center justify-start gap-1.5 rounded-lg border-transparent bg-transparent px-2 text-[12px] font-normal shadow-none hover:bg-wash"
    >
      <span className="flex h-5 min-w-0 items-center gap-x-1.5 overflow-hidden">
        <span className="h-5 truncate font-semibold leading-5 text-foreground">vibey</span>
        <span className="h-5 shrink-0 whitespace-nowrap font-mono text-[10px] leading-5 text-muted before:mr-1.5 before:font-sans before:content-['·']">{currentLabel}</span>
        <span className="h-5 shrink-0 whitespace-nowrap text-[10px] leading-5 text-muted before:mr-1.5 before:content-['·']">{t(`chat.picker.effortOptions.${props.effort}`, { defaultValue: props.effort })}</span>
      </span>
      <ChevronDown className="size-3 shrink-0 text-muted" />
    </Button>
  );
  return (
    <div className="flex h-full flex-col justify-end bg-background p-4 md:p-6">
      <div className="flex items-end gap-2 rounded-2xl border border-edge bg-surface p-3 shadow-elevation-0">
        <span className="min-w-0 flex-1 pb-2 text-[13px] text-muted">{t('chat.compose.placeholder')}</span>
        <ResponsiveMenu
          open
          onOpenChange={noop}
          trigger={trigger}
          sheetTitle={t('chat.picker.sheetTitle')}
          sheetCloseLabel={t('common.done')}
          className="w-[320px] max-w-[92vw] overflow-hidden p-0"
          align="end"
        >
          <MenuBody {...props} />
        </ResponsiveMenu>
      </div>
    </div>
  );
}

// ---- Settings › 模型 › add: the same few, first ----

export interface DraftCandidate {
  model: DraftModel;
  /** The Sources that list it, by display name. */
  suppliers: string[];
}

/** One candidate as the shipped PickerRow writes it: checkbox, name, ID, and (outside a provider fold) who lists it. */
const CandidateRow = ({ candidate, grouped = false }: { candidate: DraftCandidate; grouped?: boolean }) => (
  <button
    type="button"
    role="checkbox"
    aria-checked={false}
    className={clsx('model-hub-picker-row min-w-0', !grouped && 'model-hub-picker-row--suppliers')}
  >
    <Checkbox presentational checked={false} />
    <span className="model-hub-picker-entry">
      <span className="model-hub-picker-line flex-auto">
        <span className="model-hub-picker-name truncate">{candidate.model.label}</span>
        <span className="model-hub-picker-id truncate font-mono">{candidate.model.id}</span>
      </span>
      {!grouped && (
        <span className="model-hub-picker-chips">
          {candidate.suppliers.map((name) => (
            <span key={name} className="model-hub-pill model-hub-fill-0a border border-edge text-muted">{name}</span>
          ))}
        </span>
      )}
    </span>
  </button>
);

/** 添加 Vibey 模型 from Settings › 模型, with the new models as the first group. */
export function AddModelsDraft({ fresh, providers }: { fresh: DraftCandidate[]; providers: { name: string; rows: DraftCandidate[] }[] }) {
  const { t } = useTranslation();
  const d = useDraft();
  const supplied = providers.reduce((sum, provider) => sum + provider.rows.length, 0);
  return (
    <Dialog open onOpenChange={noop}>
      <DialogContent
        keepOnOutsideClick
        mobileSheetHeight="tall"
        closeLabel={t('common.close') as string}
        className="model-hub-catalog-dialog model-hub-picker flex h-[min(642px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-0 overflow-hidden rounded-[14px] p-0 max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
      >
        <DialogHeader className="model-hub-catalog-head shrink-0 justify-center border-b border-border">
          <DialogTitle className="model-hub-catalog-title">{t('settings.models.gateway.picker.title', { backend: 'Vibey' })}</DialogTitle>
          <DialogDescription className="sr-only">{t('settings.models.gateway.picker.description')}</DialogDescription>
        </DialogHeader>
        <div className="model-hub-catalog-body flex min-h-0 flex-1 flex-col">
          <div className="model-hub-catalog-control model-hub-catalog-search flex min-w-0 shrink-0 items-center gap-2">
            <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
            <Input
              variant="bare"
              placeholder={t('settings.models.gateway.picker.search') as string}
              aria-label={t('settings.models.gateway.picker.search') as string}
              className="min-w-0 flex-1 text-[12.5px]"
            />
          </div>
          <div className="model-hub-catalog-scroll flex min-h-0 flex-1 flex-col overflow-y-auto">
            <div className="model-hub-picker-list">
              <div role="group" aria-label={d('新模型', 'New models')} className="model-hub-picker-group" data-new-models="">
                <div className="model-hub-picker-group-head">
                  <span>{d('新模型', 'New models')}</span>
                  <span>{fresh.length}</span>
                </div>
                {fresh.map((candidate) => <CandidateRow key={candidate.model.id} candidate={candidate} />)}
              </div>
              <div role="group" aria-label={t('settings.models.gateway.picker.groupProviders') as string} className="model-hub-picker-group">
                <div className="model-hub-picker-group-head">
                  <span>{t('settings.models.gateway.picker.groupProviders')}</span>
                  <span>{supplied}</span>
                </div>
                {providers.map((provider) => (
                  <div key={provider.name} role="group" aria-label={provider.name} className="model-hub-picker-source">
                    <button type="button" aria-expanded className="model-hub-picker-source-head">
                      <ChevronRight aria-hidden="true" className="rotate-90" />
                      <span className="min-w-0 truncate">{provider.name}</span>
                      <span className="model-hub-picker-source-count">{provider.rows.length}</span>
                    </button>
                    <div className="model-hub-picker-source-rows">
                      {provider.rows.map((candidate) => <CandidateRow key={candidate.model.id} candidate={candidate} grouped />)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
        <DialogFooter className="model-hub-catalog-foot shrink-0 items-center border-t border-border max-sm:flex-col sm:justify-between">
          <Button type="button" variant="ghost" className="model-hub-catalog-control shrink-0 rounded-md px-2.5 text-[12.5px] font-semibold text-muted-foreground max-sm:w-full">
            <Plus aria-hidden="true" />
            {t('settings.models.gateway.picker.custom')}
          </Button>
          <div className="flex w-full gap-2 sm:w-auto">
            <Button type="button" variant="outline" className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none">
              {t('settings.models.gateway.catalog.cancel')}
            </Button>
            <Button type="button" variant="brand" disabled className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none">
              {t('settings.models.gateway.catalog.add')}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
