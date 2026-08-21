import { describe, expect, it } from 'vitest';

import { configMutationsToPayload } from './configMutations';
import {
  buildWizardFinishMutations,
  buildWizardStepMutations,
  collectWizardEnabledPlatformDelta,
} from './wizardConfigMutations';

describe('wizard config mutations', () => {
  it('tracks only the final intent for each wizard platform operation', () => {
    expect(collectWizardEnabledPlatformDelta([
      { kind: 'enabled-platforms', add: ['slack'], remove: ['discord'] },
      { kind: 'enabled-platforms', add: ['discord'], remove: ['slack'] },
    ])).toEqual({ add: ['discord'], remove: ['slack'] });
  });

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
      agents: { opencode: { enabled: false } },
    });
  });

  it('does not replay an installer-owned cli path on Continue', () => {
    const before = {
      agents: { opencode: { enabled: true, cli_path: 'old-path' } },
    };
    const stepData = {
      agents: { opencode: { enabled: true, cli_path: '/newly-installed/opencode' } },
    };

    expect(buildWizardStepMutations({
      stepId: 'agents',
      before,
      stepData,
      after: { ...before, ...stepData },
    })).toEqual([]);
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
    });
  });

  it('adds a platform when its credentials become runnable during the wizard', () => {
    const before = {
      platforms: { enabled: ['slack'] },
      slack: {},
    };
    const stepData = {
      slack: { bot_token: 'xoxb-token', app_token: 'xapp-token' },
    };
    const after = { ...before, ...stepData };

    expect(configMutationsToPayload(buildWizardStepMutations({
      stepId: 'platform-slack', before, stepData, after,
    }))).toEqual({
      slack: { bot_token: 'xoxb-token', app_token: 'xapp-token' },
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
        'platforms.enabled': { remove: ['discord'] },
      },
    });
  });

  it('does not re-enable a baseline platform changed by another browser', () => {
    const data = {
      platforms: { enabled: ['slack'] },
      __wizardEnabledBaseline: ['slack'],
      __wizardEnabledAdds: [],
      __wizardEnabledRemoves: [],
      slack: { has_bot_token: true, has_app_token: true },
      update: { auto_update: true },
    };

    expect(configMutationsToPayload(buildWizardFinishMutations(data, true))).toEqual({
      setup_completed: true,
    });
  });

  it('reasserts a platform that this wizard explicitly added', () => {
    const data = {
      platforms: { enabled: ['slack'] },
      __wizardEnabledBaseline: [],
      __wizardEnabledAdds: ['slack'],
      __wizardEnabledRemoves: [],
      slack: { has_bot_token: true, has_app_token: true },
      update: { auto_update: true },
    };

    expect(configMutationsToPayload(buildWizardFinishMutations(data, true))).toEqual({
      setup_completed: true,
      __avibe_list_ops: {
        'platforms.enabled': { add: ['slack'] },
      },
    });
  });

  it('does not re-enable a selected platform skipped before persistence', () => {
    const data = {
      platforms: { enabled: ['slack'] },
      __wizardEnabledBaseline: [],
      __wizardEnabledAdds: [],
      __wizardEnabledRemoves: [],
      slack: {},
      update: { auto_update: true },
    };

    expect(configMutationsToPayload(buildWizardFinishMutations(data, true))).toEqual({
      setup_completed: true,
    });
  });

  it('persists the WeChat duration override from a loaded enabled value', () => {
    const data = {
      platforms: { enabled: ['wechat'] },
      __wizardEnabledBaseline: ['wechat'],
      __wizardEnabledAdds: [],
      __wizardEnabledRemoves: [],
      show_duration: true,
      wechat: {},
      update: { auto_update: true },
    };

    expect(configMutationsToPayload(buildWizardFinishMutations(data, true))).toEqual({
      show_duration: false,
      setup_completed: true,
    });
  });
});
