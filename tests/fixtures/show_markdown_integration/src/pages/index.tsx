export default function IntegrationHome() {
  return (
    <main>
      <h1>Issue 1617 Markdown Integration</h1>
      <p id="visible-marker">Visible marker: AVIBE_MARKDOWN_VISIBLE</p>
      <p data-agent-hidden>Hidden marker: AVIBE_MARKDOWN_HIDDEN</p>
      <p agent-note="Preserve the verified release source">Annotated content</p>
      <nav>
        <a href="reports/daily?view=week&timezone=Asia%2FShanghai">Daily report</a>
        <a href="/show/ses-issue-1617/reports/daily?view=week">Same-page private link</a>
        <a href="/show/private-neighbor/secret?credential=NEVER_EXPOSE">Other private link</a>
      </nav>
    </main>
  )
}
