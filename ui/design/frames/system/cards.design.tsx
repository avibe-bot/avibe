// Card (`components/ui/card.tsx`) with its header, content, and footer slots, and Separator.
import { Cpu } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';
import { Section, Sheet, Specimen } from './sheet';

const cardsSheet = () => (
  <Sheet>
    <Section title="Card" note="rounded-xl · border-border · bg-card；槽位各自带内边距与分隔线">
      <Specimen name="Card + Header / Content / Footer" use="三段式面板" className="w-[26rem]">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <CardTitle>夜间备份</CardTitle>
              <Badge variant="success">运行中</Badge>
            </div>
            <CardDescription>每天 02:00 把工作区打包到外置硬盘，保留最近 14 份。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-[13px]">
            <div className="flex justify-between"><span className="text-muted">上次运行</span><span>今天 02:00 · 成功</span></div>
            <div className="flex justify-between"><span className="text-muted">下次运行</span><span>明天 02:00</span></div>
          </CardContent>
          <CardFooter className="justify-end gap-2">
            <Button variant="ghost" size="xs">查看日志</Button>
            <Button variant="secondary" size="xs">立即运行</Button>
          </CardFooter>
        </Card>
      </Specimen>
      <Specimen name="Card · 带图标" use="主卡片" className="w-72">
        <Card className="flex items-start gap-3 p-5">
          <span className="grid size-10 shrink-0 place-items-center rounded-lg border border-mint/30 bg-mint-soft text-mint-ink shadow-glow-sm-mint">
            <Cpu className="size-5" />
          </span>
          <div className="flex flex-col gap-1">
            <CardTitle>本机运行</CardTitle>
            <CardDescription>Agent 在你的电脑上执行，数据不离开本机。</CardDescription>
          </div>
        </Card>
      </Specimen>
    </Section>
    <Section title="Separator">
      <Specimen name='orientation="horizontal"' className="w-64">
        <div className="flex flex-col gap-3 text-[13px]">
          <span>常规</span>
          <Separator />
          <span>通知</span>
        </div>
      </Specimen>
      <Specimen name='orientation="vertical"' className="w-64">
        <div className="flex h-6 items-center gap-3 text-[13px]">
          <span>Claude Code</span>
          <Separator orientation="vertical" />
          <span>Codex</span>
          <Separator orientation="vertical" />
          <span>OpenCode</span>
        </div>
      </Specimen>
    </Section>
  </Sheet>
);

const designFrames = frames([
  { name: 'all', title: '卡片 · Card 与 Separator', width: 1280, height: 520, status: 'shipped', fixtures: systemFixtures(), render: cardsSheet },
]);

export default designFrames;
