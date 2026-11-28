// Illustrative Slack IM preview of an Agent reply carrying Session and Bot
// mentions (both with an Avibe Cloud public URL configured and without one).
// Lives in design/kit/ because Slack's client UI is external rather than a
// shipped React surface in ui/src/.

export type SlackMentionPreviewProps = {
  isEn: boolean;
  shortTitle: string;
  longTitle: string;
  botName: string;
};

export function SlackMentionPreview({
  isEn,
  shortTitle,
  longTitle,
  botName,
}: SlackMentionPreviewProps) {
  const shortUrl = 'https://demo.avibe.bot/chat/sesw34zgzgcvp';
  const longUrl = 'https://demo.avibe.bot/chat/seslongtitle1';
  return (
    <div className="flex min-h-full flex-col gap-4 bg-canvas p-5 text-foreground">
      <div className="rounded-xl border border-edge bg-surface p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between border-b border-edge pb-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-6 items-center justify-center rounded-md bg-cyan-soft text-[12px] font-bold text-cyan-ink">
              #
            </span>
            <span className="text-[13px] font-semibold">
              {isEn
                ? 'Slack · Public URL configured (linked titles)'
                : 'Slack · 已配置公网地址（标题带会话链接）'}
            </span>
          </div>
          <span className="font-mono text-[11px] text-muted">#avibe-ops</span>
        </div>

        <div className="flex items-start gap-3">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-mint-soft text-[12px] font-bold text-mint-ink">
            A
          </div>
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex items-baseline gap-2">
              <span className="text-[13px] font-semibold text-foreground">Avibe</span>
              <span className="rounded bg-field px-1.5 py-0.5 text-[10px] font-medium text-muted">APP</span>
              <span className="text-[11px] text-muted">00:54</span>
            </div>
            <p className="text-[13px] leading-[1.6] text-foreground">
              {isEn ? (
                <>
                  Done — I started a background Agent (
                  <a href={shortUrl} className="text-cyan-ink underline underline-offset-2">
                    {shortTitle}
                  </a>
                  ), handed the reconnect audit to{' '}
                  <a href={longUrl} className="text-cyan-ink underline underline-offset-2">
                    {longTitle}
                  </a>
                  , and looped in @{botName} for review. The stale id #&lt;sesmissing99&gt; from the log does not
                  resolve here so it stays plain text, and inline code{' '}
                  <code className="rounded border border-edge bg-field px-1 py-0.5 font-mono text-[11px]">
                    #&lt;sesw34zgzgcvp&gt;
                  </code>{' '}
                  is left untouched.
                </>
              ) : (
                <>
                  好，已经起了一个后台 Agent（
                  <a href={shortUrl} className="text-cyan-ink underline underline-offset-2">
                    {shortTitle}
                  </a>
                  ），并把长链路排查交给了{' '}
                  <a href={longUrl} className="text-cyan-ink underline underline-offset-2">
                    {longTitle}
                  </a>
                  ，同时抄送 @{botName} 跟进验收。另外日志里提到的旧编号 #&lt;sesmissing99&gt;
                  在当前工作区找不到，所以保留原样；文档里的示例{' '}
                  <code className="rounded border border-edge bg-field px-1 py-0.5 font-mono text-[11px]">
                    #&lt;sesw34zgzgcvp&gt;
                  </code>{' '}
                  也不会被改写。
                </>
              )}
            </p>
            <div className="pt-1 font-mono text-[11px] text-muted">
              {shortUrl} · {longUrl}
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-edge bg-surface p-4 shadow-sm">
        <div className="mb-2 flex items-center justify-between border-b border-edge pb-2">
          <span className="text-[12px] font-semibold text-muted">
            {isEn
              ? 'No public URL or plain-text platform (e.g. WeChat) · Title alone'
              : '无公网地址或不支持链接的平台（如微信）· 仅显示标题'}
          </span>
        </div>
        <p className="text-[13px] leading-[1.6] text-foreground">
          {isEn
            ? `Done — I started a background Agent (${shortTitle}), handed the reconnect audit to ${longTitle}, and looped in @${botName} for review. The stale id #<sesmissing99> stays plain text.`
            : `好，已经起了一个后台 Agent（${shortTitle}），并把长链路排查交给了 ${longTitle}，同时抄送 @${botName} 跟进验收。旧编号 #<sesmissing99> 保留原样。`}
        </p>
      </div>
    </div>
  );
}
