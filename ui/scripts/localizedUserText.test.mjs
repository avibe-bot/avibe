import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createLintTree, readConfig, writeSource } from './lintTestUtils.mjs';
import { measureLint } from './oxlintAdapter.mjs';

const RULE = 'avibe/localized-user-text';
const FILE = 'src/__probe__/LocalizedText.tsx';
// The public RuleTester requires Node >=22 even though Oxlint's CLI supports
// our Node20 floor. Batch each Vitest case's fixtures into one real CLI run on
// every platform instead: same config, native AST/scope graph, no parser shim.
let cases;
beforeEach(() => { cases = []; });
function check(code, count = 0, filename = FILE) {
  const file = path.posix.join(path.posix.dirname(filename), `__case${cases.length}__`, path.posix.basename(filename));
  cases.push({ code, count, file });
}
afterEach(() => {
  if (!cases.length) return;
  const root = createLintTree();
  try {
    for (const { code, file } of cases) writeSource(root, file, code);
    const { results, measurement } = measureLint(root);
    expect(measurement.fatals).toEqual([]);
    expect(measurement.policyGroups).toEqual([]);
    expect(measurement.inlineViolations).toEqual([]);
    expect(measurement.coverage).toEqual({ empty: false, missing: [], unwalked: [] });
    for (const { code, count, file } of cases) {
      const result = results.find((item) => item.filePath === path.join(root, file));
      expect(result, file).toBeDefined();
      const messages = result.messages.filter((message) => message.ruleId === RULE);
      expect(messages, `${file}: ${code}`).toHaveLength(count);
      expect(messages.every((message) => message.severity === 2)).toBe(true);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// Reduced from the four actual review hunks in #2559, not just inline t() calls.
// These fixtures protect the sink contract that existing lint presets never saw.
const originalRegressions = [
  ['probe reason through a local binding', `
    export function Reason({ result, outcomes, index, t }) {
      const failure = serverText(t, result?.call.error);
      const reason = outcomes[index] === 'verified' || outcomes[index] === 'unsupported'
        ? null : outcomes[index] === 'inconclusive' && !failure
          ? t('reason.inconclusive')
          : \`\${t('reason.transient')}\${failure ? \` (\${failure})\` : ''}\`;
      return <p>{reason}</p>;
    }`],
  ['cost array with spread/map and literal joiner', `
    export function Cost({ price, t }) {
      const perUnit = (usd, unit) => t('cost.perUnit', { usd, unit }) as string;
      return <span>{[t('cost.listNote'), ...price.extra.map((extra) =>
        \`+ \${perUnit(extra.usd_per_1k, extra.unit)}\`)].join(' · ')}</span>;
    }`],
  ['accessible name from props rather than inline translations', `
    export function Chip({ label, explanation, facts }) {
      return <button aria-label={[label, explanation, ...facts.map((fact) => fact.value)].join('. ')} />;
    }`],
  ['aria-hidden middle dot between translated phrases', `
    export function Summary({ upgraded, aged, t }) {
      return <p>
        {upgraded > 0 && <span>{t('upgraded', { count: upgraded })}</span>}
        {upgraded > 0 && aged > 0 && <span aria-hidden>·</span>}
        {aged > 0 && <span>{t('aged', { count: aged })}</span>}
      </p>;
    }`],
];

describe('#2618: the original localized-copy regressions cannot return unnoticed', () => {
  it.each(originalRegressions)('errors on %s', async (_name, code) => {
    check(code, 1);
  });
});

const component = (jsx, setup = '') => `
  import { useTranslation, Trans as LocaleText } from 'react-i18next';
  import { formatNameList, formatSentences, formatSmallUsd } from '@/components/settings/models/format';
  export function Probe({ user, values, ready, label, explanation, locale, n }) {
    const { t } = useTranslation();
    ${setup}
    return (${jsx});
  }
`;

// Each case names a credible escape from the sink-local contract. The four
// historical fixtures alone miss other props, aliases, control-flow and syntax.
describe('all user-facing sinks enforce literal-copy and composition boundaries', () => {
  it.each(['aria-label', 'title', 'placeholder', 'alt', 'children'])('checks literals and local composition in %s', async (prop) => {
    for (const jsx of [
      `<Widget ${prop}="Save" />`,
      `<Widget ${prop}={'Save'} />`,
      `<Widget ${prop}={sentence} />`,
      `<Widget ${prop}={joined} />`,
      `<Widget ${prop}={added} />`,
    ]) {
      const setup = "const reason = t('reason'); const sentence = `${reason} (${explanation})`; const joined = [reason, explanation].join('. '); const added = reason + ': ' + explanation;";
      check(component(jsx, setup), 1);
    }
  });

  it.each([
    ['ordinary JSX copy', '<button>Save</button>'],
    ['non-English copy', '<button>保存</button>'],
    ['literal JSX punctuation', '<p>{label} / {explanation}</p>'],
    ['explicit space between phrases', "<p>{label}{' '}{explanation}</p>"],
    ['string addition', "<p>{t('reason') + ': ' + user.name}</p>"],
    ['template with unknown data', '<p>{`Hello ${user.name}`}</p>'],
    ['literal conditional alternative', "<p>{ready ? t('ready') : 'Pending'}</p>"],
    ['literal fallback', "<p>{user.name || 'Anonymous'}</p>"],
    ['literal left fallback', "<p>{'Anonymous' ?? user.name}</p>"],
    ['conditional child', "<p>{ready && 'Pending'}</p>"],
    ['implicit comma joiner', '<p>{values.join()}</p>'],
    ['explicit undefined comma joiner', '<p>{values.join(undefined)}</p>'],
    ['computed join method', "<p>{values['join'](' / ')}</p>"],
    ['optional join call', "<p>{values?.join(' / ')}</p>"],
    ['array content with localized joiner', "<p>{['Save', t('cancel')].join(t('separator'))}</p>"],
    ['map expression result', '<p>{values.map(value => `Hello ${value}`)}</p>'],
    ['map block return result', "<p>{values.map(value => { if (ready) return value; return 'Unknown'; })}</p>"],
  ])('rejects %s', async (_name, jsx) => {
    check(component(jsx), 1);
  });

  it.each([
    ["const text = 'Save'; const alias = text as string;", '<p>{alias}</p>'],
    ["const reason = t('reason'); const failure = t('failure'); const text = `${reason} (${failure})`; const alias = text!;", '<p>{alias}</p>'],
    ["const parts = [t('a'), t('b')]; const separator = ' · '; const text = parts.filter(Boolean).join(separator);", '<p>{text}</p>'],
    ["const text = [label, explanation].join('. '); const alias = text satisfies string;", '<button aria-label={alias} />'],
    ["const callback = (value) => { const text = `Hello ${value}`; return text; }; const text = values.map(callback);", '<p>{text}</p>'],
  ])('follows immutable lexical output bindings: %s', async (setup, jsx) => {
    check(component(jsx, setup), 1);
  });

  it('does not confuse a shadowed parameter with an outer literal binding', async () => {
    const code = "const text = 'not displayed'; export function Probe({ text }) { return <p>{text}</p>; }";
    check(code);
  });

  it('terminates on cyclic bindings instead of crashing the gate', async () => {
    check(component('<p>{a}</p>', "const a = b; const b = a + 'Suffix';"), 1);
  });
});

describe('legitimate localization, dynamic data and non-copy values stay usable', () => {
  it.each([
    ['locale interpolation', "<p>{t('reasonWithFailure', { reason: t('reason'), failure: user.error })}</p>"],
    ['locale-owned cost list', "<p>{t('cost.noteWithExtras', { extras: values.map(value => t('cost.extra', { value })).join(t('cost.separator')) })}</p>"],
    ['locale-owned accessible sentence list', '<button aria-label={formatSentences([label, explanation], locale)} />'],
    ['locale-owned styled summary', '<LocaleText i18nKey="summary" values={{ upgraded: n, aged: n }} components={{ upgraded: <strong /> }} />'],
    ['locale-owned separator', "<p>{[label, explanation].filter(Boolean).join(t('separator'))}</p>"],
    ['Trans component interpolation', '<LocaleText i18nKey="pair" values={{ name: user.name }} components={{ strong: <strong /> }} />'],
    ['Trans fallback interpolation', '<LocaleText i18nKey="pair">Hello <strong>{{ name: user.name }}</strong></LocaleText>'],
    ['existing locale-aware lists', '<p>{formatNameList(values, locale)}{formatSentences(values, locale)}</p>'],
    ['existing number formatter', '<p>{formatSmallUsd(n)}</p>'],
    ['Intl formatting', '<p>{new Intl.ListFormat(locale).format(values)}{new Intl.NumberFormat(locale).format(n)}</p>'],
    ['dynamic data in every surface', '<img aria-label={label} title={user.path} placeholder={user.example} alt={user.name} />'],
    ['dynamic children and numeric arithmetic', '<p>{user.name}{n + 1}{ready && n}</p>'],
    ['non-copy attributes', '<input className="hello-world" id="field" name="field" type="text" data-label="Save" value="internal-id" />'],
    ['conditional predicate is not copy', "<p>{user.kind === 'admin' ? user.name : t('guest')}</p>"],
    ['map logging is not copy', "<p>{values.map(value => { console.log('Loaded item', value); return value; })}</p>"],
    ['empty strings', "<p>{ready ? label : ''}{values.join('')}</p>"],
  ])('allows %s', async (_name, jsx) => {
    check(component(jsx));
  });

  it('does not inspect a composition used only for protocol data or logging', async () => {
    const setup = "const text = `${user.path}/${user.id}`; console.log('Loaded: ' + text);";
    check(component('<p data-path={text}>{user.name}</p>', setup));
  });
});

describe('exemptions are narrow content contracts, not subtree escape hatches', () => {
  it.each([
    '<span aria-hidden>×</span>', '<span aria-hidden="true">→</span>',
    "<span aria-hidden={true}>{'✓'}</span>",
    '<code>model_id</code>', '<samp>https://example.com/v1</samp>',
    '<code>./config.json</code>', '<code>--verbose</code>', '<kbd>Ctrl+K</kbd>',
    '<code>{user.command}</code>',
    '<input placeholder="https://example.com/v1" />', '<span>~/.avibe/config.json</span>',
    '<pre>{\'{"scopes":["read"]}\'}</pre>',
  ])('allows explicit decorative/technical display: %s', async (jsx) => {
    check(component(jsx));
  });

  it.each([
    '<span>×</span>', '<span aria-hidden="false">×</span>',
    '<span aria-hidden={ready}>×</span>', '<span aria-hidden {...user}>×</span>', '<span aria-hidden>·</span>',
    '<span aria-hidden>Save</span>', '<span aria-hidden>✓ Save</span>',
    '<span aria-hidden><b>×</b></span>', '<span aria-hidden>✓<b>Save</b></span>',
    "<span aria-hidden>{'✓' + t('save')}</span>",
    '<span aria-hidden title="Close">×</span>', '<span aria-hidden aria-label="Close">×</span>',
    '<code>Save changes</code>', '<code>Save</code>', '<code><b>Save changes</b></code>',
    '<code title="model_id">model_id</code>', '<code aria-label="model_id">model_id</code>',
    '<kbd>Save changes</kbd>', '<p><code>model_id</code> is required</p>',
    '<pre>Save changes</pre>', '<code>{\'"Save"\'}</code>',
    '<input placeholder="Open https://example.com" />', '<p>/</p>',
    '<pre aria-label={\'{"scopes":["read"]}\'} />',
    '<LocaleText>Save</LocaleText>', '<Trans i18nKey="save">Save</Trans>',
    '<LocaleText i18nKey="save"><button title="Save">Save</button></LocaleText>',
    "<LocaleText i18nKey=\"save\">{label + ' Save'}</LocaleText>",
    '<><LocaleText i18nKey="save" />Save changes</>',
  ])('does not exempt prose, labels, or neighbors: %s', async (jsx) => {
    check(component(jsx), 1);
  });

  it('does not trust a component named Trans from another package', async () => {
    check('import { Trans } from "other"; export const Probe = () => <Trans i18nKey="save">Save</Trans>;', 1);
  });
});

describe('applicability is narrower than, but does not change, the globally pinned policy', () => {
  it.each(['src/Probe.test.tsx', 'src/Probe.spec.tsx', 'src/__tests__/Probe.tsx', 'e2e/Probe.tsx'])('does not lint test/non-source copy in %s', async (file) => {
    check('export const Probe = () => <button>Save</button>;', 0, file);
    expect(readConfig().overrides[0].rules[RULE]).toEqual([2]);
  });

  it('does not inspect literal values in non-TSX modules, while still resolving the policy', async () => {
    check('export const text = "Save";', 0, 'src/copy.ts');
    expect(readConfig().overrides[0].rules[RULE]).toEqual([2]);
  });
});

