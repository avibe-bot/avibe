// The approved system on one sheet: four levels, the state treatments that replace shadows and
// glows, and the glows that stay. Specimens are drawn with the shipped tokens and primitives.
import { Bell, Bot, Check, Inbox, Mic, MoreHorizontal, Pencil, Sparkles, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { Section, Sheet, Specimen } from '../../frames/system/sheet';

const page = 'relative flex h-44 w-64 items-center justify-center overflow-hidden rounded-xl border border-border bg-background p-4';

function Level({ level, name, use, children }: { level: string; name: string; use: string; children: React.ReactNode }) {
  return (
    <Specimen name={`${level} · ${name}`} use={use} className="w-64">
      <div className={page}>{children}</div>
    </Specimen>
  );
}

const menuRow = 'flex h-8 items-center gap-2 rounded-lg px-2 text-[13px]';

export function ElevationSheet() {
  return (
    <Sheet>
      <Section title="层级 · 阴影只表示高度" note="0 贴地 / 1 抬起 / 2 浮起 / 3 遮罩层；浮起和遮罩层同一配方：--popover 底、无描边，极淡的环在该层阴影里">
        <Level level="0" name="flat · 无阴影" use="卡片、列表行、面板、输入框：靠底色和极淡的环">
          <div className="w-full rounded-xl border border-edge bg-surface p-3 shadow-elevation-0">
            <div className="text-[13px] font-semibold">avibe</div>
            <div className="mt-1 text-[12px] text-muted">修复中途断缓存 · 12 分钟前</div>
          </div>
        </Level>
        <Level level="1" name="shadow-elevation-1" use="输入框条、分段控件滑块、开关圆钮">
          <div className="flex w-full items-center gap-2 rounded-2xl border border-edge bg-surface-2 px-3 py-2.5 shadow-elevation-1">
            <span className="flex-1 text-[12px] text-muted">和歪比说…</span>
            <span className="grid size-7 place-items-center rounded-lg bg-primary text-primary-foreground">
              <Sparkles className="size-3.5" />
            </span>
          </div>
        </Level>
        <Level level="2" name="shadow-elevation-2" use="菜单、Popover、悬停卡片、提示条、Dock">
          <div className="w-44 rounded-xl border border-edge bg-popover p-1.5 shadow-elevation-2">
            <div className={menuRow}>
              <Pencil className="size-3.5 text-muted" />
              重命名
            </div>
            <div className={cn(menuRow, 'bg-foreground/[0.04]')}>
              <MoreHorizontal className="size-3.5 text-muted" />
              复制会话链接
            </div>
            <div className={cn(menuRow, 'text-destructive-ink')}>
              <Trash2 className="size-3.5" />
              删除
            </div>
          </div>
        </Level>
        <Level level="3" name="shadow-elevation-3" use="对话框、底部面板、搜索面板、聚焦窗口">
          <div className="absolute inset-0 bg-black/40" />
          <div className="relative w-52 rounded-2xl border border-edge bg-popover p-4 shadow-elevation-3">
            <div className="text-[13px] font-semibold">重命名项目</div>
            <div className="mt-1 text-[11px] text-muted">只改变 Avibe 里的显示。</div>
            <div className="mt-3 flex justify-end gap-2">
              <span className="rounded-md px-2 py-1 text-[11px]">取消</span>
              <span className="rounded-md bg-primary px-2 py-1 text-[11px] text-primary-foreground">保存</span>
            </div>
          </div>
        </Level>
      </Section>

      <Section title="状态 · 不用阴影，也不用光晕" note="选中、未读、展开、激活：底色 + 边框；焦点：一种 2px 环">
        <Specimen name="选中行" use="--nav-selected-bg + --nav-selected-border（已有令牌，至今未用）" className="w-64">
          <div className="flex w-64 flex-col gap-1 rounded-xl border border-edge bg-surface p-2 shadow-elevation-0">
            <div className="flex items-center gap-2.5 rounded-lg border border-(--nav-selected-border) bg-(--nav-selected-bg) px-3 py-2 text-[13px] font-medium">
              <Bot className="size-4 text-mint-ink" />
              Agents
            </div>
            <div className="flex items-center gap-2.5 rounded-lg border border-transparent px-3 py-2 text-[13px] font-medium text-muted">
              <Sparkles className="size-4" />
              Skills
            </div>
          </div>
        </Specimen>
        <Specimen name="未读卡片" use="薄荷边 + 薄荷底 + 计数，去掉光晕" className="w-64">
          <div className="flex w-64 flex-col gap-2 rounded-xl border border-mint/45 bg-mint/[0.05] p-3">
            <div className="flex items-center justify-between text-[13px] font-semibold">
              首席安全官
              <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-mint px-1 font-mono text-3xs font-bold text-primary-foreground">2</span>
            </div>
            <div className="text-[12px] text-muted">发现 2 个高危漏洞，要我开 PR 吗？</div>
          </div>
        </Specimen>
        <Specimen name="焦点" use="focus-visible:ring-2 ring-ring ring-offset-2 ring-offset-background" className="w-64">
          <div className="flex h-[92px] w-64 items-center justify-center gap-3 rounded-xl border border-edge bg-surface shadow-elevation-0">
            <Button variant="outline" size="xs" className="ring-2 ring-ring ring-offset-2 ring-offset-background">
              <Check />
              标记已读
            </Button>
          </div>
        </Specimen>
        <Specimen name="悬停 / 按下" use="bg-foreground/[0.04] → /[0.06]，任何底色上都看得见" className="w-64">
          <div className="flex w-64 flex-col gap-1 rounded-xl border border-edge bg-surface p-2 shadow-elevation-0">
            <div className="rounded-lg px-3 py-2 text-[13px]">静止</div>
            <div className="rounded-lg bg-foreground/[0.04] px-3 py-2 text-[13px]">悬停</div>
            <div className="rounded-lg bg-foreground/[0.06] px-3 py-2 text-[13px]">按下</div>
          </div>
        </Specimen>
      </Section>

      <Section title="光 · 只给会发光的东西" note="颜色跟随主题自己的强调色；装饰性光晕深色保留，浅色换成同色柔影">
        <Specimen name="运行中" use="状态点保留光晕" className="w-44">
          <div className="flex h-14 items-center gap-2 text-[12px] text-muted">
            <span className="size-2 rounded-full bg-mint shadow-glow-dot-mint" />
            运行中
          </div>
        </Specimen>
        <Specimen name="正在听" use="按住说话、录音中的头像" className="w-44">
          <div className="flex h-14 items-center">
            <span className="grid size-10 place-items-center rounded-full bg-surface text-mint-ink shadow-glow-md-mint ring-2 ring-mint">
              <Mic className="size-4" />
            </span>
          </div>
        </Specimen>
        <Specimen name="品牌主按钮" use="每页一个，保持现状（2026-08-14 定）" className="w-44">
          <div className="flex h-14 items-center">
            <Button variant="brand" size="sm">
              新建 Agent
            </Button>
          </div>
        </Specimen>
        <Specimen name="装饰图标块" use="深色：光晕；浅色：同色柔影" className="w-44">
          <div className="flex h-14 items-center">
            <div className="flex size-12 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint-ink shadow-decor-md-mint">
              <Inbox className="size-5" />
            </div>
          </div>
        </Specimen>
        <Specimen name="不再发光" use="未读计数、选中行、展开卡片、普通卡片" className="w-56">
          <div className="flex h-14 items-center gap-3">
            <span className="relative">
              <Bell className="size-5 text-cyan-ink" />
              <span className="absolute -right-2 -top-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-3xs font-bold text-accent-foreground">5</span>
            </span>
          </div>
        </Specimen>
      </Section>
    </Sheet>
  );
}
