import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Plus, X } from 'lucide-react';

import { parseMcpTarget, type McpAddFormState, type McpKeyValueRow } from '../../../lib/mcpAddForm';
import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Input } from '../../ui/input';

/** Form tab of the Add MCP dialog. The parent owns state so it survives tab switches. */
export function McpAddForm({ value, onChange }: { value: McpAddFormState; onChange: (next: McpAddFormState) => void }) {
  const { t } = useTranslation();
  const [advanced, setAdvanced] = useState(Boolean(value.cwd));
  const target = parseMcpTarget(value.target);
  const isHttp = target.kind === 'http';
  const rowsLabel = isHttp ? t('mcpHub.form.headers') : t('mcpHub.form.env');
  const setRow = (index: number, patch: Partial<McpKeyValueRow>) =>
    onChange({ ...value, rows: value.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)) });

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
          <span>{rowsLabel}</span>
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={() => onChange({ ...value, rows: [...value.rows, { key: '', value: '' }] })}
          >
            <Plus className="size-3" />
            {t('mcpHub.form.addRow')}
          </Button>
        </div>
        {value.rows.map((row, index) => (
          <div key={index} className="flex items-center gap-1.5">
            <Input
              aria-label={t('mcpHub.form.key', { label: rowsLabel })}
              value={row.key}
              onChange={(event) => setRow(index, { key: event.target.value })}
              className="font-mono"
              placeholder={isHttp ? t('mcpHub.form.headerKeyPlaceholder') : t('mcpHub.form.envKeyPlaceholder')}
            />
            <Input
              aria-label={t('mcpHub.form.value', { label: rowsLabel })}
              value={row.value}
              onChange={(event) => setRow(index, { value: event.target.value })}
              className="font-mono"
            />
            <Button
              type="button"
              variant="ghost"
              size="xs"
              aria-label={t('mcpHub.form.removeRow')}
              onClick={() => onChange({ ...value, rows: value.rows.filter((_, i) => i !== index) })}
            >
              <X className="size-3" />
            </Button>
          </div>
        ))}
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
