import { useTranslation } from 'react-i18next';

import { MAX_GENERATED_STATIC_SECRET_LENGTH, type StaticSecretCharset, type StaticSecretGenerationRecipe } from '@/lib/vaultSecretGeneration';
import { Input } from './input';
import { SegmentedRadio } from './segmented';

const GROUPS = ['lowercase', 'uppercase', 'digits', 'symbols'] as const;
const GROUP_LABEL_KEYS = {
  lowercase: 'vaults.dialog.generationLowercase',
  uppercase: 'vaults.dialog.generationUppercase',
  digits: 'vaults.dialog.generationDigits',
  symbols: 'vaults.dialog.generationSymbols',
} as const satisfies Record<(typeof GROUPS)[number], string>;

export function VaultSecretGenerationOptions({
  recipe,
  onChange,
  disabled = false,
}: {
  recipe: StaticSecretGenerationRecipe;
  onChange: (recipe: StaticSecretGenerationRecipe) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const groupLabel = (group: (typeof GROUPS)[number]) => t(GROUP_LABEL_KEYS[group]);

  return (
    <div className="flex flex-col gap-2 border-y border-border py-2.5">
      <label className="flex max-w-40 flex-col gap-1">
        <span className="text-xs font-medium text-foreground">{t('vaults.dialog.generationLength')}</span>
        <Input
          type="number"
          min={1}
          max={MAX_GENERATED_STATIC_SECRET_LENGTH}
          step={1}
          value={Number.isFinite(recipe.length) ? recipe.length : ''}
          disabled={disabled}
          onChange={(event) => onChange({ ...recipe, length: event.currentTarget.valueAsNumber })}
          aria-label={t('vaults.dialog.generationLength')}
        />
      </label>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium text-foreground">{t('vaults.dialog.generationCharset')}</span>
        <SegmentedRadio<StaticSecretCharset>
          value={recipe.charset}
          onChange={(charset) => onChange({ length: recipe.length, charset })}
          disabled={disabled}
          ariaLabel={t('vaults.dialog.generationCharset')}
          options={[
            { id: 'hex', label: t('vaults.dialog.generationHex') },
            { id: 'base64url', label: t('vaults.dialog.generationBase64url') },
            { id: 'custom', label: t('vaults.dialog.generationCustom') },
          ]}
        />
      </div>
      {recipe.charset === 'custom' && (
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {GROUPS.map((group) => (
            <label key={group} className="flex items-center gap-1.5 text-xs text-foreground">
              <input
                type="checkbox"
                checked={recipe[group] ?? false}
                disabled={disabled}
                onChange={(event) => onChange({ ...recipe, [group]: event.currentTarget.checked })}
              />
              {groupLabel(group)}
            </label>
          ))}
          <label className="flex items-center gap-1.5 text-xs text-foreground">
            <input
              type="checkbox"
              checked={recipe.exclude_lookalikes ?? false}
              disabled={disabled}
              onChange={(event) => onChange({ ...recipe, exclude_lookalikes: event.currentTarget.checked })}
            />
            {t('vaults.dialog.generationExcludeLookalikes')}
          </label>
        </div>
      )}
    </div>
  );
}
