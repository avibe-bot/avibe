import { describe, expect, it } from 'vitest';

import { configMutationsToPayload } from './configMutations';
import {
  buildWizardFinishMutations,
  buildWizardStepMutations,
} from './wizardConfigMutations';

describe('wizard config mutations', () => {
  it('persists only fields owned by the Agents step', () => {
    const before = {
      agents: {
        opencode: { enabled: true, cli_path: 'old', default_agent: 'build' },
        claude: { enabled: true, cli_path: 'claude' },
      },
    };
    const stepData = {
      agents: {
        opencode: {
          enabled: false,
          cli_path: '/opt/opencode',
          default_agent: 'stale',
          status: 'ok',
        },
        claude: { enabled: true, cli_path: 'claude', status: 'missing' },
      },
    };

    expect(configMutationsToPayload(buildWizardStepMutations({
      stepId: 'agents',
      before,
      stepData,
      after: { ...before, ...stepData },
    }))).toEqual({
      agents: { opencode: { enabled: false, cli_path: '/opt/opencode' } },
    });
  });

  it('does not resend mount-time agents from a later platform step', () => {
    const before = {
      platforms: { enabled: ['slack'] },
      agents: { opencode: { cli_path: 'stale' } },
      slack: {
        has_bot_token: true,
        bot_token_length: 24,
        has_app_token: true,
        require_mention: false,
      },
    };
    const stepData = {
      platform: 'slack',
      slack: {
        has_bot_token: true,
        has_app_token: true,
        bot_token: '',
        app_token: '',
        require_mention: true,
      },
    };
    const after = { ...before, ...stepData };

    expect(configMutationsToPayload(buildWizardStepMutations({
      stepId: 'platform-slack', before, stepData, after,
    }))).toEqual({
      slack: { require_mention: true },
      __avibe_list_ops: { 'platforms.enabled': { add: ['slack'] } },
    });
  });

  it('encodes selection changes as operations', () => {
    const before = {
      platforms: { enabled: ['discord'] },
      discord: { has_bot_token: true },
      slack: { has_bot_token: true },
    };
    const stepData = { platforms: { enabled: ['slack'] } };
    const after = { ...before, ...stepData };

    expect(configMutationsToPayload(buildWizardStepMutations({
      stepId: 'platform', before, stepData, after,
    }))).toEqual({
      __avibe_list_ops: {
        'platforms.enabled': { add: ['slack'], remove: ['discord'] },
      },
    });
  });

  it('finishes setup without resending agents or platform sections', () => {
    const data = {
      platforms: { enabled: ['slack'] },
      __wizardEnabledBaseline: ['discord', 'slack'],
      slack: { has_bot_token: true },
      agents: { opencode: { cli_path: 'stale' } },
      update: { auto_update: true },
    };

    expect(configMutationsToPayload(buildWizardFinishMutations(data, false))).toEqual({
      update: { auto_update: false },
      setup_completed: true,
      __avibe_list_ops: {
        'platforms.enabled': { add: ['slack'], remove: ['discord'] },
      },
    });
  });
});
