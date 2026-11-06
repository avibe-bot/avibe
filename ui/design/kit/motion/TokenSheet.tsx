// The motion tokens (docs/plans/web-ui-motion.md §4), each shown doing what it is for: a dot crossing a
// track on that duration and curve, and the curve itself. Sizes are the spacing scale's.
import { cut, nav, progress, spring, timed, type Curve, type Track } from './loop';
import { MotionLoop } from './MotionLoop';

const START = 300;
const PERIOD = 1800;

type Row = { name: string; token: string; use: string; curve: Curve };

function rows(): { title: string; rows: Row[] }[] {
  return [
    {
      title: '时长（配 enter 曲线）',
      rows: [
        { name: 'instant', token: '--motion-duration-instant', use: '按下的反馈', curve: timed('instant', 'enter') },
        { name: 'fast', token: '--motion-duration-fast', use: '小元素：工作点、角标、图标；所有退出', curve: timed('fast', 'enter') },
        { name: 'base', token: '--motion-duration-base', use: '界面内的元素：一条消息、一行、弹出菜单', curve: timed('base', 'enter') },
        { name: 'slow', token: '--motion-duration-slow', use: '整个界面：页面、底部弹层', curve: timed('slow', 'enter') },
      ],
    },
    {
      title: '曲线（slow 时长）',
      rows: [
        { name: 'enter', token: '--motion-ease-enter', use: '进入：减速落位', curve: timed('slow', 'enter') },
        { name: 'exit', token: '--motion-ease-exit', use: '离开：加速离场（配更短的时长）', curve: timed('slow', 'exit') },
        { name: 'move', token: '--motion-ease-move', use: '已在屏上的东西换位置', curve: timed('slow', 'move') },
      ],
    },
    {
      title: '弹簧（各自的稳定时长）',
      rows: [
        { name: 'snappy', token: '--motion-spring-snappy', use: '控件：松手回弹、发送↔停止', curve: spring('snappy') },
        { name: 'smooth', token: '--motion-spring-smooth', use: '内容：消息升起、行移动', curve: spring('smooth') },
        { name: 'sheet', token: '--motion-spring-sheet', use: '界面：底部弹层', curve: spring('sheet') },
      ],
    },
    {
      title: '页面（手机上的推入与返回）',
      rows: [
        { name: 'push', token: '--motion-nav-push-duration', use: '进入会话：从右侧推入，原页面反向漂移', curve: nav('push') },
        { name: 'pop', token: '--motion-nav-pop-duration', use: '点返回：会话退回右侧；系统滑动不加动画', curve: nav('pop') },
      ],
    },
  ];
}

function Plot({ curve }: { curve: Curve }) {
  const points = Array.from({ length: 41 }, (_, i) => {
    const x = i / 40;
    return `${(x * 56).toFixed(1)},${(36 - progress(curve.ease, x) * 30).toFixed(1)}`;
  }).join(' ');
  return (
    <svg viewBox="0 0 56 40" className="h-10 w-14 shrink-0 overflow-visible" aria-hidden="true">
      <line x1="0" y1="6" x2="56" y2="6" className="stroke-border" strokeDasharray="2 2" />
      <line x1="0" y1="36" x2="56" y2="36" className="stroke-border" />
      <polyline points={points} fill="none" className="stroke-mint" strokeWidth="1.5" />
    </svg>
  );
}

export function TokenSheet() {
  const groups = rows();
  const tracks: Track[] = groups.flatMap((group) =>
    group.rows.map((row) => ({
      el: `[data-token-carrier="${row.name}"]`,
      beats: [
        // The dot rides a carrier as wide as its track less itself, so 100% is the far end.
        { at: START, to: { x: '100%' }, curve: row.curve },
        { at: PERIOD - 200, to: { opacity: 0 }, curve: timed('fast', 'exit') },
        { at: PERIOD - 30, to: { x: 0, opacity: 1 }, curve: cut },
      ],
    })),
  );
  return (
    <MotionLoop id="tokens" period={PERIOD} rest={PERIOD - 300} tracks={tracks}>
      <div className="flex h-full flex-col gap-6 overflow-hidden bg-background px-8 py-7 text-foreground">
        <div>
          <h1 className="text-lg font-bold">动效令牌</h1>
          <p className="mt-1 text-xs text-muted">src/index.css · 只动 transform 和 opacity · 每个时刻从这里取一个时长和一条曲线</p>
        </div>
        {groups.map((group) => (
          <section key={group.title} className="flex flex-col gap-3">
            <h2 className="text-xs font-bold uppercase tracking-wider text-mint-ink">{group.title}</h2>
            {group.rows.map((row) => (
              <div key={row.name} className="flex items-center gap-4">
                <div className="w-40 shrink-0">
                  <div className="font-mono text-xs font-semibold">{row.name} · {Math.round(row.curve.dur)}ms</div>
                  <div className="truncate font-mono text-2xs text-muted">{row.token}</div>
                </div>
                <Plot curve={row.curve} />
                <div className="relative h-4 w-80 shrink-0">
                  <div className="absolute inset-x-1.5 top-1/2 h-px bg-border" />
                  <div data-token-carrier={row.name} className="absolute inset-y-0 left-0 right-3">
                    <span className="absolute left-0 top-0.5 size-3 rounded-full bg-mint shadow-glow-dot-mint" />
                  </div>
                </div>
                <div className="min-w-0 flex-1 text-xs text-muted">{row.use}</div>
              </div>
            ))}
          </section>
        ))}
        <section className="flex flex-col gap-3">
          <h2 className="text-xs font-bold uppercase tracking-wider text-mint-ink">加载与交接</h2>
          <div className="flex items-center gap-4">
            <div className="w-40 shrink-0">
              <div className="font-mono text-xs font-semibold">shimmer · 1600ms</div>
              <div className="font-mono text-2xs text-muted">animate-skeleton-shimmer</div>
            </div>
            <div className="w-14 shrink-0" />
            <div className="relative h-3 w-80 shrink-0 overflow-hidden rounded-full bg-foreground/[0.06]">
              {/* Rests off the block: still under reduced motion, it shows no highlight. A transform, not a
                  `translate-x-*` utility (the separate `translate` property would add to the keyframes'). */}
              <span className="absolute inset-0 animate-skeleton-shimmer bg-linear-to-r from-transparent via-foreground/[0.03] to-transparent [transform:translateX(-100%)] motion-reduce:animate-none" />
            </div>
            <div className="min-w-0 flex-1 text-xs text-muted">骨架的高光扫过；减少动态效果时静止</div>
          </div>
          <div className="flex items-center gap-4">
            <div className="w-40 shrink-0">
              <div className="font-mono text-xs font-semibold">handover · 240ms</div>
              <div className="font-mono text-2xs text-muted">--motion-handover-*</div>
            </div>
            <div className="min-w-0 flex-1 text-xs text-muted">骨架或缓存交给内容：原位淡入，几何不变（base 时长、enter 曲线）</div>
          </div>
        </section>
      </div>
    </MotionLoop>
  );
}
