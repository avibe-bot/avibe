// The state language of a direction at real size: a Bot avatar at rest, selected (today's ring and
// the proposed lit edge), running, and unread, then the primary action and a focused control.
// The shipped BotFace and Button draw each sample; `dir-selected` (directions.css) is the proposed
// selected edge.
import type { ReactNode } from 'react';

import { Button } from '@/components/ui/button';
import { BotFace } from '@/components/workbench/bots/BotFace';
import type { BotAvatar } from '@/lib/bots';

import { useDraft } from '../draft';

const shield: BotAvatar = { kind: 'emoji', emoji: '🛡️', hue: 200 };
const mascot: BotAvatar = { kind: 'mascot' };

function Sample({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex w-[88px] flex-col items-center gap-2.5">
      <div className="grid h-12 place-items-center">{children}</div>
      <span className="text-center text-[11px] leading-tight text-muted">{label}</span>
    </div>
  );
}

export function StateSheet() {
  const d = useDraft();
  return (
    <div className="flex h-full flex-col justify-center gap-6 bg-background px-8 text-foreground">
      <div className="flex items-start gap-2">
        <Sample label={d('默认', 'Rest')}>
          <BotFace avatar={shield} name="CSO" size={40} />
        </Sample>
        <Sample label={d('今天的选中：外圈', 'Selected today')}>
          <span className="rounded-full p-[2px] shadow-[0_0_0_2px_var(--mint)]">
            <BotFace avatar={shield} name="CSO" size={40} />
          </span>
        </Sample>
        <Sample label={d('选中：边缘亮起', 'Selected: lit edge')}>
          <BotFace avatar={shield} name="CSO" size={40} className="dir-selected" />
        </Sample>
        <Sample label={d('歪比 · 选中', 'Main Bot selected')}>
          <BotFace avatar={mascot} name="Vibey" size={40} className="dir-selected" />
        </Sample>
        <Sample label={d('运行中', 'Running')}>
          <BotFace avatar={shield} name="CSO" size={40} running dotWell="bg-background" />
        </Sample>
        <Sample label={d('未读', 'Unread')}>
          <BotFace avatar={shield} name="CSO" size={40} unread={2} />
        </Sample>
        <Sample label={d('选中 + 运行中', 'Selected, running')}>
          <BotFace avatar={mascot} name="Vibey" size={40} running dotWell="bg-background" className="dir-selected" />
        </Sample>
      </div>
      <div className="flex items-center gap-3">
        <Button>{d('新建 Agent', 'New Agent')}</Button>
        <Button variant="outline">{d('次要操作', 'Secondary')}</Button>
        <Button variant="outline" className="ring-2 ring-ring ring-offset-2 ring-offset-background">
          {d('键盘焦点', 'Keyboard focus')}
        </Button>
      </div>
    </div>
  );
}
