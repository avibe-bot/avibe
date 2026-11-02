// The main Bot's conversation is home (owner direction, 2026-11-02). Empty, it greets
// by the Bot's name and offers what the old new-conversation home did, as things to
// say to the main Bot: a tap fills the composer, nothing is sent until the user
// sends. There is no project to pick: the main Bot starts work in a project itself.
// Drawn over the shipped empty Bot conversation, which it replaces for the main Bot.
import { BookmarkPlus, FolderKanban, Rocket, UserPlus } from 'lucide-react';
import clsx from 'clsx';

import { BotAvatar } from '@/components/ui/bot-avatar';
import { useBots } from '@/context/BotsContext';

import { useDraft } from './draft';

export function MainBotWelcome({ compact = false }: { compact?: boolean }) {
  const draft = useDraft();
  const { mainBot, displayName } = useBots();
  if (!mainBot) return null;
  const name = displayName(mainBot);
  const suggestions = [
    { icon: UserPlus, title: draft('添加一位伙伴', 'Add a Bot'), say: draft('帮我加一个负责安全的伙伴', 'Add a Bot that handles security') },
    { icon: FolderKanban, title: draft('看看项目进展', 'Check on projects'), say: draft('各个项目现在进展怎么样？', 'How are the projects going?') },
    { icon: Rocket, title: draft('在项目里开始一件事', 'Start work in a project'), say: draft('在 avibe 里开个会话，把缓存问题修掉', 'Open a session in avibe and fix the cache bug') },
    { icon: BookmarkPlus, title: draft('记住一件事', 'Remember something'), say: draft('记住：周一上午不排会', 'Remember: no meetings on Monday mornings') },
  ];
  return (
    <div className={clsx('flex w-full flex-col items-center gap-5 rounded-3xl border border-border bg-surface text-center shadow-mint-card-sm', compact ? 'max-w-sm p-5' : 'max-w-xl p-7')}>
      <BotAvatar avatar={mainBot.avatar} name={name} size={compact ? 56 : 64} />
      <div className="flex flex-col gap-1.5">
        <h1 className={clsx('font-bold text-foreground', compact ? 'text-lg' : 'text-2xl')}>
          {draft(`你好，我是${name}`, `Hi, I'm ${name}`)}
        </h1>
        <p className="text-sm leading-relaxed text-muted">
          {draft('想做什么直接跟我说。我自己做，或者交给合适的伙伴和 Agent，做完告诉你。', 'Tell me what you need. I do it, or hand it to the right Bot or Agent, and tell you when it is done.')}
        </p>
      </div>
      <div className={clsx('grid w-full gap-2 text-left', compact ? 'grid-cols-1' : 'grid-cols-2')}>
        {suggestions.map(({ icon: Icon, title, say }) => (
          <button
            key={title}
            type="button"
            className="group flex items-start gap-2.5 rounded-2xl border border-border-strong bg-surface-2 px-3.5 py-3 text-left transition hover:border-mint/40 hover:bg-mint-soft"
          >
            <Icon className="mt-0.5 size-4 shrink-0 text-mint-ink" />
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-sm font-semibold text-foreground">{title}</span>
              <span className="truncate text-xs text-muted">{say}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Where the welcome sits: centered between a conversation's header and its composer. */
export function WelcomeLayer({ compact = false }: { compact?: boolean }) {
  return (
    <div className={clsx('pointer-events-none absolute inset-x-0 z-50 flex items-center justify-center px-6', compact ? 'top-14 bottom-36' : 'top-16 bottom-44')}>
      <div className="pointer-events-auto flex w-full justify-center">
        <MainBotWelcome compact={compact} />
      </div>
    </div>
  );
}
