// Layers that open over a page: Dialog (a bottom sheet on phones), ConfirmDialog, Popover, InfoHint,
// the cursor ContextMenu, and ResponsiveMenu (a popover on desktop, a sheet on phones). Hover hints
// elsewhere are native `title` tooltips, which a screenshot cannot draw.
import { Copy, ExternalLink, FolderOpen, MoreHorizontal, Pencil, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { ContextMenu, ContextMenuItem } from '@/components/ui/context-menu';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { InfoHint } from '@/components/ui/info-hint';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';

import { frames } from '../../frame';
import { systemFixtures } from './fixtures';

const noop = () => {};

const dialog = () => (
  <Dialog open>
    <DialogContent closeLabel="关闭">
      <DialogHeader>
        <DialogTitle>重命名项目</DialogTitle>
        <DialogDescription>新名称只改变 Avibe 里的显示，不会移动磁盘上的文件夹。</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-2">
        <Label htmlFor="system-dialog-name">项目名称</Label>
        <Input id="system-dialog-name" defaultValue="avibe-docs" />
      </div>
      <DialogFooter>
        <Button variant="ghost">取消</Button>
        <Button>保存</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
);

const confirm = () => (
  <ConfirmDialog
    open
    onOpenChange={noop}
    onConfirm={noop}
    destructive
    title="删除会话「周报整理」？"
    description="会话记录和它生成的文件会一起删除，无法恢复。"
    confirmLabel="删除"
  />
);

const popover = () => (
  <div className="p-8">
    <Popover open>
      <PopoverTrigger asChild>
        <Button variant="secondary" size="sm">远程访问</Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="flex flex-col gap-2">
        <p className="text-[13px] font-semibold">已通过 Avibe Cloud 连接</p>
        <p className="text-[12px] leading-relaxed text-muted">在手机上打开 lin.avibe.bot 即可访问这台电脑上的工作台。</p>
        <Button size="xs" variant="outline" className="self-start"><ExternalLink />打开远程地址</Button>
      </PopoverContent>
    </Popover>
  </div>
);

const infoHint = () => (
  <div className="flex items-center gap-1.5 p-8 text-[15px] font-semibold">
    模型网关
    <InfoHint label="模型网关是什么？" content="模型网关在本机为各个 Agent 统一提供模型：登录、API Key 与路由都在一处管理，Agent 不用各自配置。" />
  </div>
);

const contextMenu = () => (
  <ContextMenu x={24} y={24} onClose={noop} width={220} itemCount={5}>
    <ContextMenuItem icon={<FolderOpen className="size-3.5" />} label="在新标签页打开" shortcut="⌘-click" onClick={noop} />
    <ContextMenuItem icon={<Pencil className="size-3.5" />} label="重命名" onClick={noop} />
    <ContextMenuItem icon={<Copy className="size-3.5" />} label="复制路径" onClick={noop} />
    <ContextMenuItem icon={<Trash2 className="size-3.5" />} label="删除" danger onClick={noop} />
  </ContextMenu>
);

const menuRow = 'flex w-full items-center rounded-md px-2.5 py-2 text-left text-[12px] font-semibold hover:bg-surface-2';

const responsiveMenu = () => (
  <div className="flex justify-end p-8">
    <ResponsiveMenu
      open
      onOpenChange={noop}
      sheetTitle="周报整理"
      sheetDescription="avibe · 3 分钟前"
      className="w-44"
      trigger={<Button size="icon" variant="ghost" aria-label="更多操作"><MoreHorizontal /></Button>}
    >
      {/* Rows as the shipped menus write them (SourceManageMenu). */}
      <button type="button" role="menuitem" className={menuRow}>重命名</button>
      <button type="button" role="menuitem" className={menuRow}>复制会话链接</button>
      <button type="button" role="menuitem" className={cn(menuRow, 'text-destructive-ink hover:bg-destructive/[0.08]')}>删除</button>
    </ResponsiveMenu>
  </div>
);

const fixtures = systemFixtures();

const designFrames = frames([
  { name: 'dialog', title: '浮层 · 对话框', width: 900, height: 480, status: 'shipped', fixtures, render: dialog },
  { name: 'dialog-phone', title: '浮层 · 对话框（手机底部面板）', width: 390, height: 640, status: 'shipped', fixtures, render: dialog },
  { name: 'confirm', title: '浮层 · 确认删除', width: 900, height: 420, status: 'shipped', fixtures, render: confirm },
  { name: 'popover', title: '浮层 · Popover', width: 480, height: 260, status: 'shipped', fixtures, render: popover },
  {
    name: 'info-hint',
    title: '浮层 · InfoHint 说明',
    width: 420,
    height: 220,
    status: 'shipped',
    fixtures,
    click: ['[aria-label="模型网关是什么？"]'],
    render: infoHint,
  },
  { name: 'context-menu', title: '浮层 · 右键菜单', width: 320, height: 220, status: 'shipped', fixtures, render: contextMenu },
  { name: 'menu', title: '浮层 · 响应式菜单（桌面）', width: 800, height: 260, status: 'shipped', fixtures, render: responsiveMenu },
  { name: 'menu-phone', title: '浮层 · 响应式菜单（手机）', width: 390, height: 520, status: 'shipped', fixtures, render: responsiveMenu },
]);

export default designFrames;
