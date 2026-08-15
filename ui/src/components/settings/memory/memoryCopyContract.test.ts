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

  it.each(['en', 'zh'] as const)('keeps staged IM attachment disclosure separate in %s', (language) => {
    const settings = BUNDLES[language].memory.settings;
    const baseDisclosure = settings.disclosure.join('\n');

    if (language === 'en') {
      expect(baseDisclosure).not.toContain('eligible attachments from bound direct messages');
      expect(settings.disclosureAttachment).toContain('eligible attachments from bound direct messages');
    } else {
      expect(baseDisclosure).not.toContain('已绑定私聊中的合规附件');
      expect(settings.disclosureAttachment).toContain('已绑定私聊中的合规附件');
    }
  });

  it.each(['en', 'zh'] as const)('discloses proactive non-plain-content capture in %s', (language) => {
    const disclosure = BUNDLES[language].memory.settings.disclosure.join('\n');

    if (language === 'en') {
      expect(disclosure).toContain('proactively save durable notes without being asked');
      expect(disclosure).toMatch(/files.*non-plain content/i);
    } else {
      expect(disclosure).toContain('未明确要求');
      expect(disclosure).toContain('主动保存');
      expect(disclosure).toMatch(/文件.*非纯文本/);
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

  it('presents reinitialization roots as mixed storage locations with secondary technical paths', () => {
    expect(en.memory.factoryReset.confirmDescription).toBe(
      'This permanently deletes local Memory data and related operational state, then attempts to start a brand-new Memory engine. Even if deletion succeeds, the new engine may fail to start and the old data will not be restored. Memory settings, credentials, the installed runtime, and original chats are kept.',
    );
    expect(en.memory.factoryReset.deletesTitle).toBe('This attempts to permanently delete:');
    expect(en.memory.factoryReset.roots.primaryStorage).toEqual({
      label: 'Primary Memory storage',
      description: 'May include profiles, facts, indexes, call diagnostics, and runtime files',
    });
    expect(en.memory.factoryReset.roots.memoryStateStorage).toEqual({
      label: 'Memory state storage',
      description: 'May include processing queues, recovery progress, and coordination state',
    });
    expect(en.memory.factoryReset.technicalPath).toContain('{{path}}');

    expect(zh.memory.factoryReset.confirmDescription).toBe(
      '这会永久删除本机记忆数据和相关运行状态，然后尝试启动一个全新的记忆引擎。即使数据删除成功，新引擎也可能无法启动，旧数据不会恢复。记忆设置、凭据、已安装的运行时和原始聊天记录会保留。',
    );
    expect(zh.memory.factoryReset.deletesTitle).toBe('将尝试永久删除：');
    expect(zh.memory.factoryReset.roots.primaryStorage).toEqual({
      label: '主要记忆存储',
      description: '可能包含画像、事实、索引、调用诊断和运行文件',
    });
    expect(zh.memory.factoryReset.roots.memoryStateStorage).toEqual({
      label: '记忆状态存储',
      description: '可能包含处理队列、恢复进度和协调状态',
    });
    expect(zh.memory.factoryReset.technicalPath).toContain('{{path}}');
  });

  it.each(['en', 'zh'] as const)('keeps operational guidance scoped to runtime behavior in %s', (language) => {
    const bundle = BUNDLES[language];
    const disclosure = bundle.memory.settings.disclosure.join('\n');

    if (language === 'en') {
      expect(bundle.memory.processingRecord.runtime.noneDisabled).toBe('No separately disabled features reported.');
      expect(bundle.memory.processingRecord.anomalies.help).toContain("won't recover automatically");
      expect(bundle.memory.processingRecord.anomalies.help).toContain('Manual review required');
      expect(disclosure).toContain('raw messages and attachment copies');
      expect(disclosure).toContain('every user and project');
      expect(bundle.memory.clear.confirmDescription).toContain('every user and project');
      expect(bundle.memory.clear.removes[0]).toContain('every user and project');
    } else {
      expect(bundle.memory.processingRecord.runtime.noneDisabled).toBe('未报告单独禁用的功能。');
      expect(bundle.memory.processingRecord.anomalies.help).toContain('不会自动恢复');
      expect(bundle.memory.processingRecord.anomalies.help).toContain('需要人工检查');
      expect(disclosure).toContain('原始消息和附件副本');
      expect(disclosure).toContain('所有用户和项目');
      expect(bundle.memory.clear.confirmDescription).toContain('所有用户和项目');
      expect(bundle.memory.clear.removes[0]).toContain('所有用户和项目');
    }
  });

  it.each(['en', 'zh'] as const)('keeps Search, source timestamps, and processing-log scope accurate in %s', (language) => {
    if (language === 'en') {
      expect(en.memory.search.description).toContain('Default');
      expect(en.memory.search.projectAll).toBe('All my projects');
      expect(en.memory.search.partial).toContain('incomplete');
      expect(en.memory.processingRecord.sources.help).toContain('last checked');
      expect(en.memory.processingRecord.sources.help).not.toContain('last updated');
      expect(en.memory.log.description).toBe(
        'See the processing history for created Memory entries across every user and project on this installation.',
      );
      expect(en.memory.clear.confirmDescription).toContain('Avibe-managed Memory data');
    } else {
      expect(zh.memory.search.description).toContain('Default');
      expect(zh.memory.search.projectAll).toBe('我的全部项目');
      expect(zh.memory.search.partial).toContain('不完整');
      expect(zh.memory.processingRecord.sources.help).toContain('最近一次检查');
      expect(zh.memory.processingRecord.sources.help).not.toContain('上次更新');
      expect(zh.memory.log.description).toBe('查看本安装中所有用户和项目的已创建记忆条目的处理记录。');
      expect(zh.memory.clear.confirmDescription).toContain('Avibe 在本机上管理的记忆数据');
    }
  });
});
