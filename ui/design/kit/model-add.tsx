// Proposal: adding a model to a backend, from where the models are listed.
//
// Two pieces. `ModelListAddRow` is the last row of a backend's model list on Settings · Models, so
// adding starts where the list is read. `AddModelDialog` is the one dialog every entry opens: it
// searches the providers' models and the model catalog together, and a search that finds nothing to
// add ends in a row that adds what was typed as a custom model. It wears the shipped picker's classes, so its rows are the
// picker's rows.
import { Plus, Search } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

import { useDraft } from './draft';

/** The last row of a backend's model list: opens the add dialog for that backend. */
export function ModelListAddRow() {
  const d = useDraft();
  return (
    <Button type="button" variant="outline" size="sm" className="h-10 w-full rounded-md text-[12px] font-semibold">
      <Plus aria-hidden="true" />
      {d('添加模型', 'Add model')}
    </Button>
  );
}

export type AddModelRow = {
  id: string;
  name?: string;
  /** The providers that list it. Empty for a catalog-only model. */
  providers?: string[];
  picked?: boolean;
};

const pill = 'model-hub-pill model-hub-fill-0a border border-edge text-muted';

function Row({ row, listed = false, unlisted = false, single = false }: { row: AddModelRow; listed?: boolean; unlisted?: boolean; single?: boolean }) {
  const d = useDraft();
  const checked = Boolean(row.picked) || listed;
  const chips = unlisted ? [d('供应商未列出', 'Not listed by a provider')] : row.providers ?? [];
  return (
    <button
      type="button"
      role={single ? 'radio' : 'checkbox'}
      aria-checked={checked}
      disabled={listed}
      className={cn(
        'model-hub-picker-row min-w-0',
        row.picked && 'is-picked',
        listed && 'model-hub-picker-row--listed cursor-not-allowed',
        chips.length > 0 && 'model-hub-picker-row--suppliers',
      )}
    >
      <Checkbox presentational checked={checked} disabled={listed} />
      <span className="model-hub-picker-entry">
        <span className="model-hub-picker-line flex-auto">
          {row.name ? (
            <>
              <span className="model-hub-picker-name truncate">{row.name}</span>
              <span className="model-hub-picker-id truncate font-mono">{row.id}</span>
            </>
          ) : (
            <span className="model-hub-picker-name model-hub-picker-name--id truncate font-mono">{row.id}</span>
          )}
        </span>
        {chips.length > 0 && (
          <span className="model-hub-picker-chips">
            {chips.map((chip) => <span key={chip} className={pill}>{chip}</span>)}
          </span>
        )}
      </span>
    </button>
  );
}

function Group({ label, note, rows, listed, unlisted, single }: { label: string; note?: string; rows: AddModelRow[]; listed?: boolean; unlisted?: boolean; single?: boolean }) {
  if (rows.length === 0) return null;
  return (
    <div role="group" aria-label={label} className="model-hub-picker-group">
      <div className="model-hub-picker-group-head">
        <span>{label}</span>
        <span>{rows.length}</span>
      </div>
      {note && <p className="px-1 text-[11px] leading-[16px] text-muted">{note}</p>}
      {rows.map((row) => <Row key={row.id} row={row} listed={listed} unlisted={unlisted} single={single} />)}
    </div>
  );
}

/** Adds exactly what was typed, so a search that finds nothing to add still ends in an action. */
function CustomFromQuery({ query, prominent = false }: { query: string; prominent?: boolean }) {
  const d = useDraft();
  return (
    <button
      type="button"
      className={cn(
        'flex min-h-10 w-full min-w-0 items-center gap-2.5 rounded-lg border border-dashed px-3 py-2 text-left',
        prominent ? 'border-mint bg-mint-soft' : 'border-border',
      )}
    >
      <Plus className={cn('size-4 shrink-0', prominent ? 'text-foreground' : 'text-muted')} aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12.5px] font-semibold text-foreground">
          {d('把「', 'Add "')}<span className="font-mono">{query}</span>{d('」添加为自定义模型', '" as a custom model')}
        </span>
        <span className="block text-[11px] leading-[16px] text-muted">
          {d('模型 ID 按你输入的原样使用，参数以后可以在「管理模型」里改', 'The model ID is used exactly as typed. Its parameters can be changed later in Manage models.')}
        </span>
      </span>
    </button>
  );
}

export function AddModelDialog({
  backend,
  choose = 'many',
  query = '',
  builtin = [],
  providers = [],
  catalog = [],
  listed = [],
}: {
  backend: string;
  /** `one`: setup choosing an assistant's model. */
  choose?: 'many' | 'one';
  query?: string;
  builtin?: AddModelRow[];
  providers?: AddModelRow[];
  /** Models the catalog knows that no provider lists. Shown only for a search. */
  catalog?: AddModelRow[];
  listed?: AddModelRow[];
}) {
  const d = useDraft();
  const single = choose === 'one';
  const picked = [...builtin, ...providers, ...catalog].filter((row) => row.picked).length;
  const addable = builtin.length + providers.length + catalog.length;
  const nothing = addable + listed.length === 0;
  const searching = query !== '';
  return (
    <Dialog open>
      <DialogContent
        mobileSheetHeight="tall"
        closeLabel={d('关闭', 'Close')}
        // The shipped picker opens with an empty search; a filled one must not open selected.
        onOpenAutoFocus={(event) => event.preventDefault()}
        className="model-hub-catalog-dialog model-hub-picker flex h-[min(642px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-0 overflow-hidden rounded-[14px] p-0 max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
      >
        <DialogHeader className="model-hub-catalog-head shrink-0 justify-center border-b border-border">
          <DialogTitle className="model-hub-catalog-title">
            {single ? d(`选择 ${backend} 的模型`, `Choose a ${backend} model`) : d(`添加 ${backend} 模型`, `Add ${backend} models`)}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {d('搜索你的供应商和模型目录，或把输入的 ID 添加为自定义模型。', 'Search your providers and the model catalog, or add the ID you typed as a custom model.')}
          </DialogDescription>
        </DialogHeader>

        <div className="model-hub-catalog-body flex min-h-0 flex-1 flex-col">
          <div className="model-hub-catalog-control model-hub-catalog-search flex min-w-0 shrink-0 items-center gap-2">
            <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
            <Input
              variant="bare"
              readOnly
              value={query}
              placeholder={d('搜索模型名称、ID 或供应商', 'Search by model name, ID, or provider')}
              aria-label={d('搜索模型名称、ID 或供应商', 'Search by model name, ID, or provider')}
              className="min-w-0 flex-1 text-[12.5px]"
            />
          </div>

          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            {nothing ? (
              <div className="model-hub-picker-empty px-2">
                <p>{d('你的供应商和模型目录里都没有这个模型。', 'Neither your providers nor the model catalog has this model.')}</p>
                {searching && <CustomFromQuery query={query} prominent />}
              </div>
            ) : (
              <div className="model-hub-picker-list">
                <Group label={d(`${backend} 内置`, `${backend} built-in`)} rows={builtin} single={single} />
                <Group label={d('来自你的供应商', 'From your providers')} rows={providers} single={single} />
                <Group
                  label={d('模型目录', 'Model catalog')}
                  note={d(
                    '你的供应商没有列出这些模型。添加后按原 ID 发给供应商；供应商不支持时，对话里会告诉你。',
                    'Your providers do not list these models. Once added, the ID is sent to the provider as is; the chat tells you if the provider does not serve it.',
                  )}
                  rows={catalog}
                  unlisted
                  single={single}
                />
                <Group label={d('已在列表中', 'Already in the list')} rows={listed} listed single={single} />
                {/* Only when nothing that can be added matched: then what was typed is most likely an ID. */}
                {searching && addable === 0 && <CustomFromQuery query={query} />}
              </div>
            )}
          </div>
        </div>

        <DialogFooter className="model-hub-catalog-foot shrink-0 items-center border-t border-border max-sm:flex-col sm:justify-between">
          <Button
            type="button"
            variant="ghost"
            className="model-hub-catalog-control shrink-0 rounded-md px-2.5 text-[12.5px] font-semibold text-muted-foreground max-sm:w-full"
          >
            <Plus aria-hidden="true" />
            {d('添加自定义模型…', 'Add custom model…')}
          </Button>
          <div className="flex w-full gap-2 sm:w-auto">
            <Button type="button" variant="outline" className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none">
              {d('取消', 'Cancel')}
            </Button>
            <Button type="button" variant="brand" disabled={picked === 0} className="model-hub-catalog-control flex-1 rounded-md px-5 text-[12.5px] font-semibold sm:flex-none">
              {single
                ? d('使用此模型', 'Use this model')
                : picked === 0
                  ? d('添加模型', 'Add models')
                  : d(`添加 ${picked} 个模型`, picked === 1 ? 'Add 1 model' : `Add ${picked} models`)}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
