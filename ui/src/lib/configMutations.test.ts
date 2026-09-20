import { describe, expect, it } from 'vitest';

import {
  configChanges,
  configMutationsToPayload,
  setConfigField,
  updateEnabledPlatforms,
} from './configMutations';

describe('config mutations', () => {
  it('derives only changed leaf fields from a stale UI snapshot', () => {
    const before = {
      agents: { opencode: { enabled: true, cli_path: '/old/opencode' } },
      ui: { show_tool_calls: true },
    };
    const after = {
      agents: { opencode: { enabled: false, cli_path: '/old/opencode' } },
      ui: { show_tool_calls: true },
    };

    expect(configMutationsToPayload(configChanges(before, after))).toEqual({
      agents: { opencode: { enabled: false } },
    });
  });

  it('serializes enabled-platform deltas separately from config data', () => {
    expect(
      configMutationsToPayload([
        setConfigField(['slack', 'bot_token'], 'token'),
        updateEnabledPlatforms({ add: ['slack', 'wechat'], remove: ['discord', 'slack'] }),
      ]),
    ).toEqual({
      slack: { bot_token: 'token' },
      __avibe_list_ops: {
        'platforms.enabled': { add: ['slack', 'wechat'], remove: ['discord'] },
      },
    });
  });

  it('rejects snapshot-shaped enabled-list replacement', () => {
    expect(() =>
      configMutationsToPayload([setConfigField(['platforms', 'enabled'], ['slack'])]),
    ).toThrow('updateEnabledPlatforms');
  });

  it('rejects empty, unsafe, and conflicting mutation sets', () => {
    expect(() => configMutationsToPayload([])).toThrow('cannot be empty');
    expect(() =>
      configMutationsToPayload([
        setConfigField(['agents'], { opencode: { enabled: false } }),
      ]),
    ).toThrow('must target a leaf value');
    expect(() =>
      configMutationsToPayload([setConfigField(['__proto__', 'polluted'], true)]),
    ).toThrow('Invalid config mutation path');
    expect(() =>
      configMutationsToPayload([
        setConfigField(['ui'], 'invalid-parent'),
        setConfigField(['ui', 'show_tool_calls'], true),
      ]),
    ).toThrow('Conflicting config mutation path');
  });
});
