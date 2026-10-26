import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const RULE = 'avibe/localized-user-text';
let root;

// Test-owned mini UI under this worktree's dependency directory: normal Node
// resolution reuses installed dependencies, but no source/config/build output
// is shared or mutated. The outer gate excludes node_modules by contract.
beforeEach(() => {
  root = fs.mkdtempSync(path.join(UI_ROOT, 'node_modules/.localized-lint-'));
  fs.mkdirSync(path.join(root, 'scripts'));
  fs.mkdirSync(path.join(root, 'src'));
  fs.copyFileSync(path.join(UI_ROOT, 'eslint.config.js'), path.join(root, 'eslint.config.js'));
  for (const name of ['lint-baseline.mjs', 'lintPolicy.mjs', 'browserGlobals.mjs', 'localizedUserText.mjs']) {
    fs.copyFileSync(path.join(UI_ROOT, 'scripts', name), path.join(root, 'scripts', name));
  }
  write('package.json', JSON.stringify({ type: 'module', scripts: { lint: 'node scripts/lint-baseline.mjs' } }));
  write('eslint-baseline.json', JSON.stringify({ rationale: { [RULE]: 'Test-owned legacy copy.' }, violations: {}, suppressions: {} }));
});
afterEach(() => fs.rmSync(root, { recursive: true, force: true }));
const write = (name, value) => fs.writeFileSync(path.join(root, name), value);
const baseline = () => JSON.parse(fs.readFileSync(path.join(root, 'eslint-baseline.json'), 'utf8'));
function gate(...args) {
  const result = spawnSync('npm', ['run', 'lint', '--silent', '--', ...args], { cwd: root, encoding: 'utf8', timeout: 30000 });
  if (result.error) throw result.error;
  return { status: result.status, output: `${result.stdout}\n${result.stderr}` };
}
const COPY = 'export const Probe = () => <button>Save</button>;\n';
const LOCALIZED = `import { useTranslation } from 'react-i18next';
export function Probe() { const { t } = useTranslation(); return <button>{t('save')}</button>; }\n`;

describe('the real npm lint gate ratchets localized-copy debt without a second ledger', () => {
  it('rejects new files and increased counts; requires paid-down debt to be re-recorded', () => {
    write('src/Legacy.tsx', COPY);
    expect(gate().output).toContain('NEW       src/Legacy.tsx  avibe/localized-user-text  1');
    expect(gate('--update').status).toBe(0);
    expect(baseline().violations).toEqual({ 'src/Legacy.tsx': { [RULE]: 1 } });
    expect(gate().status).toBe(0);

    write('src/New.tsx', COPY);
    let result = gate();
    expect(result.status).toBe(1);
    expect(result.output).toContain('NEW       src/New.tsx');
    fs.rmSync(path.join(root, 'src/New.tsx'));

    write('src/Legacy.tsx', 'export const Probe = () => <><button>Save</button><button>Cancel</button></>;');
    result = gate();
    expect(result.status).toBe(1);
    expect(result.output).toContain('EXPANDED  src/Legacy.tsx');

    write('src/Legacy.tsx', LOCALIZED);
    result = gate();
    expect(result.status).toBe(1);
    expect(result.output).toContain('STALE     src/Legacy.tsx');
    expect(gate('--update').status).toBe(0);
    expect(baseline().violations).toEqual({});
    expect(gate().status).toBe(0);

    write('src/Legacy.tsx', COPY);
    expect(gate().status).toBe(1);
  }, 60000);

  it('keeps the documented same-file/same-rule equal-count replacement limitation explicit', () => {
    write('src/Legacy.tsx', COPY);
    expect(gate('--update').status).toBe(0);
    write('src/Legacy.tsx', 'export const Probe = () => <img alt="Different unlocalized text" />;');
    expect(gate().status).toBe(0);
  }, 20000);

  it('does not turn disable comments, inline overrides or config demotion into unmeasured slack', () => {
    write('src/New.tsx', `// eslint-disable-next-line ${RULE}\n${COPY}`);
    let result = gate();
    expect(result.status).toBe(1);
    expect(result.output).toContain('1 suppressed messages (not in the baseline)');

    write('src/New.tsx', `/* eslint ${RULE}: "off" */\n${COPY}`);
    result = gate();
    expect(result.status).toBe(1);
    expect(result.output).toContain('INLINE');

    write('src/New.tsx', LOCALIZED);
    const config = fs.readFileSync(path.join(root, 'eslint.config.js'), 'utf8');
    write('eslint.config.js', config.replace("'avibe/localized-user-text': 'error'", "'avibe/localized-user-text': 'warn'"));
    const before = fs.readFileSync(path.join(root, 'eslint-baseline.json'), 'utf8');
    result = gate('--update');
    expect(result.status).toBe(1);
    expect(result.output).toContain('POLICY');
    expect(result.output).toContain('avibe/localized-user-text: expected [2], resolved [1]');
    expect(fs.readFileSync(path.join(root, 'eslint-baseline.json'), 'utf8')).toBe(before);
  }, 30000);
});
