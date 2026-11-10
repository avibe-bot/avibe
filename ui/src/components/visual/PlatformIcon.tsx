import * as React from 'react';
import { cn } from '@/lib/utils';
import avibeLogo from '@/assets/cloud-tuanzi-logo-mono.png';

export type PlatformId = 'slack' | 'discord' | 'telegram' | 'lark' | 'wechat' | string;

interface PlatformIconProps extends React.HTMLAttributes<HTMLSpanElement> {
  platform: PlatformId;
  size?: number;
  /** When true, renders a tinted square tile around the icon. */
  tile?: boolean;
}

const SlackSvg: React.FC<{ size: number }> = ({ size }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
    <path d="M9.4 18.2a2.6 2.6 0 1 1-2.6-2.6h2.6v2.6Z" fill="#E01E5A" />
    <path d="M10.7 18.2a2.6 2.6 0 0 1 5.2 0v6.5a2.6 2.6 0 1 1-5.2 0v-6.5Z" fill="#E01E5A" />
    <path d="M13.3 9.4a2.6 2.6 0 1 1 2.6-2.6v2.6h-2.6Z" fill="#36C5F0" />
    <path d="M13.3 10.7a2.6 2.6 0 0 1 0 5.2H6.8a2.6 2.6 0 1 1 0-5.2h6.5Z" fill="#36C5F0" />
    <path d="M22.6 13.3a2.6 2.6 0 1 1 2.6 2.6h-2.6v-2.6Z" fill="#2EB67D" />
    <path d="M21.3 13.3a2.6 2.6 0 0 1-5.2 0V6.8a2.6 2.6 0 1 1 5.2 0v6.5Z" fill="#2EB67D" />
    <path d="M18.7 22.6a2.6 2.6 0 1 1-2.6 2.6v-2.6h2.6Z" fill="#ECB22E" />
    <path d="M18.7 21.3a2.6 2.6 0 0 1 0-5.2h6.5a2.6 2.6 0 1 1 0 5.2h-6.5Z" fill="#ECB22E" />
  </svg>
);

const DiscordSvg: React.FC<{ size: number }> = ({ size }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
    <path
      fill="#5865F2"
      d="M25.6 7.2a23 23 0 0 0-5.7-1.8l-.3.6a18 18 0 0 0-5.2 0l-.3-.6a23 23 0 0 0-5.7 1.8C5 12.5 4.2 17.6 4.5 22.7c2.2 1.6 4.4 2.6 6.5 3.2.5-.7 1-1.4 1.4-2.2-.7-.3-1.5-.6-2.1-1l.5-.4c4 1.9 8.4 1.9 12.4 0l.5.4-2.1 1c.4.8.9 1.5 1.4 2.2 2.1-.6 4.3-1.6 6.5-3.2.4-5.9-.7-11-3.5-15.5ZM12.4 19.6c-1.1 0-2-1-2-2.3s.9-2.3 2-2.3 2 1 2 2.3-.9 2.3-2 2.3Zm7.2 0c-1.1 0-2-1-2-2.3s.9-2.3 2-2.3 2 1 2 2.3-.9 2.3-2 2.3Z"
    />
  </svg>
);

const TelegramSvg: React.FC<{ size: number }> = ({ size }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="tgGrad" x1=".5" x2=".5" y2="1">
        <stop offset="0%" stopColor="#37AEE2" />
        <stop offset="100%" stopColor="#1E96C8" />
      </linearGradient>
    </defs>
    <circle cx="16" cy="16" r="16" fill="url(#tgGrad)" />
    <path
      fill="#FFF"
      d="m23.4 9.8-2.7 13c-.2 1-.7 1.2-1.5.8l-4-3-1.9 1.9c-.3.2-.5.4-.9.4l.3-4.3 7.7-7c.4-.3-.1-.5-.5-.2l-9.5 6-4.1-1.3c-.9-.3-.9-.9.2-1.4L22 8.6c.7-.3 1.4.2 1.4 1.2Z"
    />
  </svg>
);

const LarkSvg: React.FC<{ size: number }> = ({ size }) => (
  <svg width={size} height={size} viewBox="-5 -8 42 42" xmlns="http://www.w3.org/2000/svg">
    <path
      fill="#4BC0AE"
      d="M16.59,13.32l.08-.08c.05-.05.11-.11.16-.16l.11-.11.32-.32.45-.43.38-.38.36-.35.37-.37.34-.34.48-.47c.09-.09.18-.18.28-.26.17-.16.35-.31.53-.46.4-.32.83-.61,1.27-.88.25-.15.52-.29.78-.42.39-.19.8-.36,1.21-.49.07-.02.15-.05.23-.07-.66-2.59-1.87-5.02-3.54-7.11C20.06.23,19.57,0,19.05,0H5.37c-.14,0-.26.12-.26.26,0,.08.04.16.1.21,4.67,3.42,8.54,7.82,11.34,12.89l.03-.04h0Z"
    />
    <path
      fill="#4C6EB5"
      d="M11.15,25.37c7.07,0,13.23-3.9,16.43-9.66.11-.2.22-.41.33-.61-.21.42-.47.81-.75,1.18-.16.21-.34.41-.52.61-.25.27-.53.51-.82.73-.12.09-.24.18-.37.27-.16.11-.33.21-.5.31-.5.28-1.03.5-1.58.65-.28.08-.56.14-.84.18-.2.03-.41.05-.62.07-.22.02-.44.02-.66.02-.25,0-.5-.02-.74-.05-.18-.02-.37-.05-.55-.08-.16-.03-.32-.06-.48-.1-.09-.02-.17-.04-.25-.07-.23-.06-.47-.13-.7-.2-.12-.04-.23-.07-.35-.1-.17-.05-.35-.1-.52-.16-.14-.04-.28-.09-.42-.14-.13-.04-.27-.09-.4-.13l-.27-.09-.33-.12-.23-.09c-.16-.05-.31-.11-.47-.17-.09-.04-.18-.07-.27-.1l-.36-.14-.38-.15-.25-.1-.3-.13-.23-.1-.24-.11-.21-.09-.19-.09-.2-.09-.2-.09-.25-.12-.27-.13c-.09-.05-.19-.09-.28-.14l-.24-.12C7.45,13.84,3.65,11.01.45,7.58c-.1-.1-.26-.11-.37,0-.05.05-.08.11-.08.18v12.08s0,.98,0,.98c0,.57.28,1.1.75,1.42,3.07,2.05,6.69,3.14,10.39,3.14h0Z"
    />
    <path
      fill="#214295"
      d="M31.92,8.34c-2.49-1.22-5.35-1.44-7.99-.6-.07.02-.15.05-.23.07-.69.24-1.36.54-1.99.91-.26.15-.51.32-.76.49-.37.26-.72.54-1.05.84-.09.09-.18.17-.28.26l-.48.47-.34.34-.37.37-.36.35-.38.38-.44.44-.32.32-.11.11c-.05.05-.11.11-.16.16l-.08.08-.12.11-.14.13c-1.18,1.08-2.49,2.01-3.9,2.76l.25.12.2.09.2.09.19.09.21.09.24.11.23.1.3.13.25.1.38.15c.12.05.24.09.36.14.09.04.18.07.27.1.16.06.31.11.46.17l.23.09c.11.04.22.08.33.12l.27.09c.13.04.27.09.4.13.14.05.28.09.42.14.17.05.35.11.52.16.35.1.7.2,1.05.3.09.02.17.04.25.07.16.04.32.07.48.1.18.03.37.06.55.08.67.08,1.36.06,2.03-.04.28-.04.56-.1.84-.18.35-.1.7-.22,1.03-.37.28-.12.54-.27.8-.43.09-.05.16-.11.24-.16.13-.09.25-.17.37-.27.11-.08.21-.16.31-.25.38-.33.73-.7,1.03-1.1.28-.37.53-.76.75-1.17l.18-.36,1.63-3.25.02-.04c.53-1.16,1.27-2.21,2.18-3.1Z"
    />
  </svg>
);

const WeChatSvg: React.FC<{ size: number }> = ({ size }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
    <path
      fill="#07C160"
      d="M12.2 6.5c-5 0-9 3.4-9 7.5 0 2.4 1.4 4.5 3.5 5.9.2.1.3.4.2.6l-.4 1.5c-.1.3.2.5.4.4l1.9-1.1c.2-.1.4-.1.6 0 .9.3 2 .5 2.9.5h.5c-.1-.5-.2-1.1-.2-1.7 0-3.8 3.7-6.9 8.1-6.9h.7c-.5-3.5-4.2-6.7-9.2-6.7Zm-3 4.4a1 1 0 1 1 0 2 1 1 0 0 1 0-2Zm5.9 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z"
    />
    <path
      fill="#07C160"
      d="M28 19.7c0-3.4-3.4-6.1-7.5-6.1S13 16.3 13 19.7c0 3.5 3.4 6.1 7.5 6.1.8 0 1.7-.1 2.4-.4.2 0 .3 0 .5.1l1.7 1c.2.1.4-.1.4-.3l-.4-1.4c0-.2 0-.4.2-.5 1.7-1.2 2.7-3 2.7-4.6Zm-9.7-1.6a.9.9 0 1 1 0-1.8.9.9 0 0 1 0 1.8Zm5 0a.9.9 0 1 1 0-1.8.9.9 0 0 1 0 1.8Z"
    />
  </svg>
);

const FALLBACK_LABELS: Record<string, string> = {
  slack: 'SL',
  discord: 'DC',
  telegram: 'TG',
  lark: 'LK',
  feishu: 'LK',
  wechat: 'WX',
};

const renderInner = (platform: string, size: number) => {
  switch (platform) {
    case 'slack':
      return <SlackSvg size={size} />;
    case 'discord':
      return <DiscordSvg size={size} />;
    case 'telegram':
      return <TelegramSvg size={size} />;
    case 'lark':
    case 'feishu':
      return <LarkSvg size={size} />;
    case 'wechat':
      return <WeChatSvg size={size} />;
    case 'avibe':
      return (
        <img
          src={avibeLogo}
          alt=""
          width={size}
          height={size}
          className="object-contain"
        />
      );
    default:
      return (
        <span className="font-mono text-[10px] font-bold uppercase tracking-wider">
          {FALLBACK_LABELS[platform] || platform.slice(0, 2).toUpperCase()}
        </span>
      );
  }
};

export const PlatformIcon: React.FC<PlatformIconProps> = ({
  platform,
  size = 24,
  tile = false,
  className,
  ...props
}) => {
  const inner = renderInner(platform, size);
  if (!tile) {
    return (
      <span
        className={cn('inline-flex shrink-0 items-center justify-center', className)}
        style={{ width: size, height: size }}
        {...props}
      >
        {inner}
      </span>
    );
  }
  const tileSize = size + 16;
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-xl border border-edge bg-surface-2 shadow-elevation-0',
        className
      )}
      style={{ width: tileSize, height: tileSize }}
      {...props}
    >
      {inner}
    </span>
  );
};
