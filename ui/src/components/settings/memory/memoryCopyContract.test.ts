import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';

const BUNDLES = { en, zh } as const;

describe('Memory UI copy contracts', () => {
  it.each(['en', 'zh'] as const)('describes runtime capabilities beyond models in %s', (language) => {
    const text = BUNDLES[language].memory.processingRecord.runtime.capabilitiesHelp;

    if (language === 'en') {
      expect(text).toMatch(/file parsing/i);
      expect(text).toMatch(/search/i);
      expect(text).toMatch(/knowledge/i);
      expect(text).not.toMatch(/kinds of models/i);
    } else {
      expect(text).toContain('文件解析');
      expect(text).toContain('搜索');
      expect(text).toContain('知识库');
      expect(text).not.toContain('模型类型');
    }
  });

  it.each(['en', 'zh'] as const)('discloses outbound search and diagnostic retention in %s', (language) => {
    const disclosure = BUNDLES[language].memory.settings.disclosure.join('\n');

    expect(disclosure).toMatch(/5,000/);
    if (language === 'en') {
      expect(disclosure).toMatch(/Search sends queries/);
      expect(disclosure).toMatch(/14 days/);
      expect(disclosure).toContain('Avibe-managed');
    } else {
      expect(disclosure).toContain('搜索查询');
      expect(disclosure).toContain('14 天');
      expect(disclosure).toContain('Avibe 在本机上管理');
    }
  });

  it.each(['en', 'zh'] as const)('keeps rebuild cost guidance conditional in %s', (language) => {
    const text = BUNDLES[language].memory.settings.rebuildConfirmDescription;
    expect(text).toMatch(language === 'en' ? /may use/ : /可能消耗/);
  });

  it('keeps processing terminology aligned with the runtime contracts', () => {
    expect(en.memory.processingRecord.runtime.fact.cascade.optimizeFailureStreak).not.toMatch(/cleanup/i);
    expect(en.memory.processingRecord.runtime.fact.cascadeReason.optimizeStuck).toMatch(/Optimization/);
    expect(en.memory.log.callStage.cascade).not.toMatch(/queue/i);
    expect(zh.memory.processingRecord.runtime.fact.cascade.optimizeFailureStreak).not.toContain('清理');
    expect(zh.memory.processingRecord.runtime.fact.cascadeReason.optimizeStuck).toContain('优化');
    expect(zh.memory.log.callStage.cascade).not.toContain('队列');
  });

  it.each(['en', 'zh'] as const)('limits Search and processing-log descriptions to returned data in %s', (language) => {
    if (language === 'en') {
      expect(en.memory.search.description).toBe('Search your profile, episodes, and facts.');
      expect(en.memory.log.description).toBe('See the processing history for created Memory entries.');
      expect(en.memory.clear.confirmDescription).toContain('Avibe-managed Memory data');
    } else {
      expect(zh.memory.search.description).toBe('搜索你的画像、事件和事实。');
      expect(zh.memory.log.description).toBe('查看已创建记忆条目的处理记录。');
      expect(zh.memory.clear.confirmDescription).toContain('Avibe 在本机上管理的记忆数据');
    }
  });
});
