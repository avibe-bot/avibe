import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';

const bundles = { en, zh } as const;

describe('Memory UI copy contracts', () => {
  it.each(['en', 'zh'] as const)('explains how an ungenerated profile will develop in %s', (language) => {
    const emptyProfile = bundles[language].memory.profile.warningEmpty;
    expect(emptyProfile).toMatch(language === 'en' ? /hasn't been generated yet/i : /画像尚未生成/);
    expect(emptyProfile).toMatch(language === 'en' ? /more content/i : /积累更多内容/);
    expect(emptyProfile).toMatch(language === 'en' ? /Avibe will try to generate/i : /Avibe 会尝试/);
    expect(emptyProfile).not.toMatch(language === 'en' ? /provider|support/i : /提供方|支持/);
  });

  it.each(['en', 'zh'] as const)('names runtime recovery by user intent in %s', (language) => {
    const runtimeAction = bundles[language].memory.runtimeAction;
    expect(runtimeAction.retryButton).toBe(language === 'en' ? 'Retry startup' : '重试启动');
    expect(runtimeAction.restartButton).toBe(language === 'en' ? 'Restart Memory service' : '重启记忆服务');
    expect(runtimeAction.restartDescription).toMatch(language === 'en' ? /without deleting Memory data/ : /不会删除记忆数据/);
  });

  it.each(['en', 'zh'] as const)('makes Repair destructive and loss-explicit in %s', (language) => {
    const repair = bundles[language].memory.repair;
    if (language === 'en') {
      expect(repair.confirmDescription).toContain('permanently deletes');
      expect(repair.confirmDescription).toContain('confined local Memory data roots');
      expect(repair.confirmLabel).toContain('Accept loss');
      expect(repair.confirmDescription).toContain('stable identity');
    } else {
      expect(repair.confirmDescription).toContain('永久删除');
      expect(repair.confirmDescription).toContain('受限的本地记忆数据根目录');
      expect(repair.confirmLabel).toContain('接受丢失');
      expect(repair.confirmDescription).toContain('稳定身份');
    }
  });

  it.each(['en', 'zh'] as const)('keeps Delete data distinct from Repair in %s', (language) => {
    const memory = bundles[language].memory;
    expect(memory.deleteData.button).not.toBe(memory.repair.button);
    expect(memory.deleteData.confirmTitle).not.toBe(memory.repair.confirmTitle);
    expect(memory.deleteData.confirmDescription).toMatch(
      language === 'en' ? /every user and project/ : /所有用户和项目/,
    );
  });

  it.each(['en', 'zh'] as const)('keeps model identity transition copy scope-neutral in %s', (language) => {
    const settings = bundles[language].memory.settings;
    const transitionCopy = `${settings.organizationTransitionTitle}\n${settings.organizationTransitionDescription}`;

    expect(transitionCopy).not.toMatch(language === 'en' ? /organization/i : /组织/);
    expect(transitionCopy).toMatch(language === 'en' ? /model identity/i : /模型身份/);
  });

  it.each(['en', 'zh'] as const)('does not expose removed recovery copy in %s', (language) => {
    const memory = bundles[language].memory as Record<string, unknown>;
    const processingRecord = memory.processingRecord as Record<string, unknown>;
    const runtime = processingRecord.runtime as Record<string, unknown>;
    const status = memory.status as Record<string, unknown>;
    const localeText = JSON.stringify(memory);

    expect(memory).not.toHaveProperty('factoryReset');
    expect(memory).not.toHaveProperty('clear');
    expect(memory).not.toHaveProperty('wake');
    expect(processingRecord).not.toHaveProperty('repair');
    expect(processingRecord).not.toHaveProperty('clearInProgress');
    expect(runtime).not.toHaveProperty('cascade');
    expect(status).not.toHaveProperty('restartEngine');
    expect(localeText).not.toMatch(/Wake Memory|唤醒记忆|Retry rebuild|重试重建|Factory Reset|恢复出厂|Restart engine|重启引擎/);
  });

  it.each(['en', 'zh'] as const)('keeps best-effort capture disclosure in %s', (language) => {
    const disclosure = bundles[language].memory.settings.disclosure.join('\n');
    if (language === 'en') {
      expect(disclosure).toContain('bounded and process-local');
      expect(disclosure).toContain('not queued durably');
      expect(disclosure).toContain('ambiguous provider outcomes are not replayed');
      expect(disclosure).toContain('native Processing Records');
    } else {
      expect(disclosure).toContain('有界且仅由当前进程管理');
      expect(disclosure).toContain('不会进入持久队列');
      expect(disclosure).toContain('不会重放');
      expect(disclosure).toContain('原生处理记录');
    }
  });

  it.each(['en', 'zh'] as const)('localizes the unified runtime errors in %s', (language) => {
    for (const key of [
      'memory_wake_failed',
      'memory_loss_confirmation_required',
      'memory_local_data_unusable',
      'memory_repair_not_required',
      'memory_delete_data_failed',
    ] as const) {
      expect(bundles[language].errors[key]).toBeTruthy();
    }
  });
});
