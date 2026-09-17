import type { ComponentProps } from 'react';
import type { TFunction } from 'i18next';
import { createInstance } from 'i18next';
import { describe, expect, it } from 'vitest';
import type { ProxyUrlField } from '../components/shared/ProxyUrlField';
import type { WorkbenchModulePlaceholder } from '../components/workbench/WorkbenchModulePlaceholder';
import type { AppDefinition } from '../apps/registry';
import type { SourceStatePresentation } from '../components/settings/models/sourceStatePresentation';
import { FilesApiError, fileBrowserErrorMessage } from '../lib/filesApi';
import { platformText } from '../lib/platforms';
import type { TranslationKey } from './types';
import type { TranslationPaths } from './resourceTypes';
import en from './en.json';
import zh from './zh.json';

// Compiled by typecheck:tests, not executed. These are real consuming prop/data
// types: broadening a carrier or t makes an expect-error unused and fails CI.
function compilerContract(t: TFunction, key: TranslationKey, custom: boolean) {
  const proxy: ComponentProps<typeof ProxyUrlField> = {
    value: '', onChange: () => {}, labelKey: 'common.proxyUrl', hintKey: 'common.proxyUrlHint',
  };
  // @ts-expect-error typo in an actual component carrier
  proxy.labelKey = 'common.proxyUrll';
  // @ts-expect-error object prefixes cannot be rendered labels
  proxy.labelKey = 'common';
  // @ts-expect-error a list cannot be rendered as a text label
  proxy.hintKey = 'memory.settings.disclosure';
  const app: Pick<AppDefinition, 'titleKey'> = { titleKey: 'apps.fileBrowser.label' };
  // @ts-expect-error typo in the app registry data contract
  app.titleKey = 'apps.fileBrowser.lable';
  const state: Pick<SourceStatePresentation, 'key'> = { key: 'settings.models.upstream.state.standby' };
  // @ts-expect-error a source label must be a text leaf
  state.key = 'settings.models';
  const module: ComponentProps<typeof WorkbenchModulePlaceholder> = { icon: null, i18nPrefix: 'workbench.modules.skills' };
  // @ts-expect-error prefix lacks both required text children
  module.i18nPrefix = 'common';
  // @ts-expect-error a plural-only family requires count, so is not a plain label
  proxy.labelKey = 'settings.models.gateway.agentIssues.summary';
  proxy.labelKey = 'settings.models.gateway.agentIssues.summary_one';
  const text: string = t(key);
  const title: string = t(`${module.i18nPrefix}.title`);
  const plural: string = t('settings.models.gateway.agentIssues.summary', { count: 2 });
  const list = t(custom ? 'memory.settings.disclosure' : 'memory.settings.cloudDisclosure', { returnObjects: true });
  const lengths: number[] = list.map((line) => line.length);
  // @ts-expect-error string resource cannot satisfy the real list consumption
  t('common.save', { returnObjects: true }).map((line: string) => line);
  // @ts-expect-error object resource cannot satisfy the real list consumption
  t('common', { returnObjects: true }).map((line: string) => line);
  // @ts-expect-error unknown direct name needs an explicit fallback
  t('common.svae');
  const fallback: string = t('deliberately.absent', { defaultValue: 'Fallback' });
  const parent: string = t('settings.models.addKey.field.vendor');
  const child: string = t('settings.models.addKey.field.vendor.search');
  const sub: string = t('settings.models.direct.card.current.sub');
  // @ts-expect-error dotted copy is a string, never String.search
  t('settings.models.addKey.field.vendor.search')('query');
  // @ts-expect-error dotted copy must not acquire String.search's callable signature
  const method: (query: string | RegExp) => number = t('settings.models.addKey.field.vendor.search');
  // @ts-expect-error dotted copy must not acquire String.sub's callable signature
  const subMethod: () => string = t('settings.models.direct.card.current.sub');
  // @ts-expect-error no phantom string-method descendants
  t('settings.models.addKey.field.vendor.toUpperCase');
  // @ts-expect-error no phantom descendants under a literal dotted key
  t('settings.models.addKey.field.vendor.search.typo');
  return [proxy, app, state, text, title, plural, lengths, fallback, parent, child, sub, method, subMethod];
}
void compilerContract;

const collision = { leaf: { title: 'Nested' }, 'leaf.title': ['Literal'] };
function collisionContract(value: TranslationPaths<typeof collision>['leaf.title']) {
  const text: string = value;
  // @ts-expect-error nested path wins: the literal sibling cannot make this an array
  value.map((line: string) => line);
  return text;
}
void collisionContract;

const translator = (lng: 'en' | 'zh') => {
  const instance = createInstance();
  void instance.init({ lng, fallbackLng: false, resources: { en: { translation: en }, zh: { translation: zh } } });
  return instance;
};

const dottedEntries = (resource: object, prefix = ''): [string, unknown][] =>
  Object.entries(resource).flatMap(([key, value]) => [
    ...(key.includes('.') ? [[`${prefix}${key}`, value] as [string, unknown]] : []),
    ...(value && typeof value === 'object' && !Array.isArray(value) ? dottedEntries(value, `${prefix}${key}.`) : []),
  ]);

describe('resource type representation and dynamic boundaries', () => {
  it.each(['en', 'zh'] as const)('resolves every literal dotted sibling in %s', (lng) => {
    const instance = translator(lng);
    const entries = dottedEntries(lng === 'en' ? en : zh);
    expect(entries.length).toBeGreaterThan(0);
    for (const [key, value] of entries) {
      expect(instance.t(key, { defaultValue: '', returnObjects: true }), key).toEqual(value);
    }
    expect(instance.t('settings.models.addKey.field.vendor')).toEqual((lng === 'en' ? en : zh).settings.models.addKey.field.vendor);
    expect(instance.t('memory.settings.disclosure', { returnObjects: true })).toEqual((lng === 'en' ? en : zh).memory.settings.disclosure);
    expect(instance.t('common', { returnObjects: true }).save).toEqual((lng === 'en' ? en : zh).common.save);
  });

  it('matches nested-before-literal collision precedence', () => {
    const instance = createInstance();
    void instance.init({ lng: 'en', resources: { en: { translation: { collision } } } });
    expect(instance.t('collision.leaf.title', { defaultValue: '', returnObjects: true })).toBe('Nested');
  });

  it('keeps absent-key defaults explicit', () => {
    expect(translator('en').t('deliberately.absent', { defaultValue: 'Fallback' })).toBe('Fallback');
  });

  it.each(['en', 'zh'] as const)('resolves file errors and preserves unknown/error-shape fallback in %s', (lng) => {
    const instance = translator(lng);
    expect(fileBrowserErrorMessage(new FilesApiError('not_found', 'raw'), instance.t, 'fallback')).toBe(instance.t('apps.fileBrowser.errors.not_found'));
    expect(fileBrowserErrorMessage(new FilesApiError('future', 'raw'), instance.t, 'fallback')).toBe('raw');
    expect(fileBrowserErrorMessage(new Error('ordinary'), instance.t, 'fallback')).toBe('ordinary');
    expect(fileBrowserErrorMessage(null, instance.t, 'fallback')).toBe('fallback');
    instance.addResourceBundle(lng, 'translation', { apps: { fileBrowser: { errors: { object: { title: 'Object' }, list: ['Array'] } } } }, true, true);
    for (const code of ['object', 'list']) expect(fileBrowserErrorMessage(new FilesApiError(code, 'raw'), instance.t, 'fallback')).toBe('raw');
  });

  it('resolves open platform keys and retains the established key fallback', () => {
    const instance = translator('en');
    expect(platformText(instance.t, 'slack', 'title')).toBe(instance.t('platform.slack.title'));
    expect(platformText(instance.t, 'future', 'title')).toBe('platform.future.title');
    expect(platformText(instance.t, 'slack', 'title', '')).toBe(instance.t('platform.slack.title'));
    expect(platformText(instance.t, 'slack', 'title', 'future.key')).toBe('future.key');
    expect(platformText(instance.t, 'slack', 'title', 'common')).toBe('common');
    expect(platformText(instance.t, 'slack', 'title', 'memory.settings.disclosure')).toBe('memory.settings.disclosure');
  });
});
