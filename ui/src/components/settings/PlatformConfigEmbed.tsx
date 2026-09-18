import type React from 'react';
import { SlackConfig } from '@/components/steps/SlackConfig';
import { DiscordConfig } from '@/components/steps/DiscordConfig';
import { TelegramConfig } from '@/components/steps/TelegramConfig';
import { LarkConfig } from '@/components/steps/LarkConfig';
import { WeChatConfig } from '@/components/steps/WeChatConfig';

export const PlatformConfigEmbed: React.FC<{
  platform: string;
  config: Record<string, unknown>;
  onApply: (data: Record<string, unknown>) => Promise<void>;
  onCancel: () => void;
}> = ({ platform, config, onApply, onCancel }) => {
  const noopNext = () => {};
  if (platform === 'slack') {
    return <SlackConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'discord') {
    return <DiscordConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'telegram') {
    return <TelegramConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'lark') {
    return <LarkConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'wechat') {
    return (
      <WeChatConfig
        data={config}
        onNext={noopNext}
        embedded
        onApply={onApply}
        onCancel={onCancel}
        autoStartLogin={false}
      />
    );
  }
  return null;
};
