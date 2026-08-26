import { describe, expect, it } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

import { backendRuntimeApplyError } from './useBackendRuntime';

describe('backendRuntimeApplyError', () => {
  it('localizes package-mutation contention by stable restart code', () => {
    expect(
      backendRuntimeApplyError(
        {
          agent_backend_runtime: {
            restart_code: 'restart_not_scheduled_package_busy',
            restart_error: 'a package operation is in progress; restart was not scheduled',
          },
        },
        'Save failed',
        '已有软件包操作正在进行，未安排重启。',
      ),
    ).toBe('已有软件包操作正在进行，未安排重启。');
  });

  it('preserves backend error text for other restart codes', () => {
    expect(
      backendRuntimeApplyError(
        {
          agent_backend_runtime: {
            restart_code: 'restart_spawn_failed',
            restart_error: 'supervisor unavailable',
          },
        },
        'Save failed',
        'localized busy message',
      ),
    ).toBe('supervisor unavailable');
  });

  it('ships the package-mutation message in both UI locales', () => {
    expect(en.settings.agentBackend.packageMutationBusy).toBe(
      'A package operation is in progress; restart was not scheduled.',
    );
    expect(zh.settings.agentBackend.packageMutationBusy).toBe(
      '已有软件包操作正在进行，未安排重启。',
    );
  });
});
