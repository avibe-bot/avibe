import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Plus, X } from 'lucide-react';

import { parseMcpTarget, type McpAddFormState, type McpKeyValueRow } from '../../../lib/mcpAddForm';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Input } from '../../ui/input';

/** Key/value inputs of an env or header map (Add form, Configure dialog); `secret` masks typed values. */
export function McpKeyValueRows({
  label,
  rows,
  onChange,
  keyPlaceholder,
  secret = false,
}: {
  label: string;
  rows: McpKeyValueRow[];
  onChange: (next: McpKeyValueRow[]) => void;
  keyPlaceholder: string;
  secret?: boolean;
}) {
  const { t } = useTranslation();
  const setRow = (index: number, patch: Partial<McpKeyValueRow>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  return (
    <>
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <Input
            aria-label={t('mcpHub.form.key', { label })}
            value={row.key}
            onChange={(event) => setRow(index, { key: event.target.value })}
            className="font-mono"
            placeholder={keyPlaceholder}
          />
          <Input
            aria-label={t('mcpHub.form.value', { label })}
            type={secret ? 'password' : undefined}
            autoComplete={secret ? 'off' : undefined}
            value={row.value}
            onChange={(event) => setRow(index, { value: event.target.value })}
            className="font-mono"
          />
          <Button
            type="button"
            variant="ghost"
            size="xs"
            aria-label={t('mcpHub.form.removeRow')}
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          >
            <X className="size-3" />
          </Button>
        </div>
      ))}
    </>
  );
}

/** Form tab of the Add MCP dialog. The parent owns state so it survives tab switches. */
export function McpAddForm({ value, onChange }: { value: McpAddFormState; onChange: (next: McpAddFormState) => void }) {
  const { t } = useTranslation();
  const [advanced, setAdvanced] = useState(Boolean(value.cwd));
  const target = parseMcpTarget(value.target);
  const isHttp = target.kind === 'http';
  const rowsLabel = isHttp ? t('mcpHub.form.headers') : t('mcpHub.form.env');
  // Env and header rows stay separate so flipping the input never turns stdio secrets into remote headers.
  const rowsKey = isHttp ? 'headerRows' : 'envRows';
  const rows = value[rowsKey];
  const setRows = (next: McpKeyValueRow[]) => onChange({ ...value, [rowsKey]: next });
  // null follows the content: collapsed when empty, expanded once rows exist.
  const [rowsToggled, setRowsToggled] = useState<boolean | null>(null);
  const rowsOpen = rowsToggled ?? rows.length > 0;

  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-[12px] text-foreground">
        {t('mcpHub.form.name')}
        <Input value={value.name} onChange={(event) => onChange({ ...value, name: event.target.value })} placeholder={t('mcpHub.form.namePlaceholder')} />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-foreground">
        <span className="flex items-center gap-2">
          {t('mcpHub.form.target')}
          {target.kind === 'http' || target.kind === 'stdio' ? (
            <Badge className="font-mono text-[9px] uppercase">{isHttp ? t('mcpHub.form.kindHttp') : t('mcpHub.form.kindStdio')}</Badge>
          ) : null}
        </span>
        <Input
          value={value.target}
          onChange={(event) => onChange({ ...value, target: event.target.value })}
          className="font-mono"
          placeholder={t('mcpHub.form.targetPlaceholder')}
        />
        {target.kind === 'error' ? (
          <span className="text-[11px] text-destructive-ink">{t(`mcpHub.form.issues.${target.issue}`)}</span>
        ) : null}
      </label>
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between text-[12px] text-foreground">
          <button
            type="button"
            className="flex items-center gap-1 text-[12px] text-foreground"
            aria-expanded={rowsOpen}
            onClick={() => setRowsToggled(!rowsOpen)}
          >
            {rowsOpen ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
            {t('mcpHub.form.rowsToggle', { label: rowsLabel, count: rows.length })}
          </button>
          {rowsOpen ? (
            <Button type="button" variant="outline" size="xs" onClick={() => setRows([...rows, { key: '', value: '' }])}>
              <Plus className="size-3" />
              {t('mcpHub.form.addRow')}
            </Button>
          ) : null}
        </div>
        {rowsOpen ? (
          <McpKeyValueRows
            label={rowsLabel}
            rows={rows}
            onChange={setRows}
            keyPlaceholder={isHttp ? t('mcpHub.form.headerKeyPlaceholder') : t('mcpHub.form.envKeyPlaceholder')}
          />
        ) : null}
      </div>
      {target.kind === 'stdio' ? (
        <div className="flex flex-col gap-1.5">
          <button
            type="button"
            className="flex items-center gap-1 self-start text-[12px] text-muted hover:text-foreground"
            aria-expanded={advanced}
            onClick={() => setAdvanced((open) => !open)}
          >
            {advanced ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
            {t('mcpHub.form.advanced')}
          </button>
          {advanced ? (
            <label className="flex flex-col gap-1 text-[12px] text-foreground">
              {t('mcpHub.form.cwd')}
              <Input value={value.cwd} onChange={(event) => onChange({ ...value, cwd: event.target.value })} className="font-mono" />
            </label>
          ) : null}
        </div>
      ) : null}
      <p className="text-[11px] text-muted">{t('mcpHub.form.hint')}</p>
    </div>
  );
}
