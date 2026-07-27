/**
 * FROZEN CONTRACT — Show Page annotation chat row.
 *
 * Both lanes build against this file, not against prose. Lane BE produces rows
 * that satisfy it; Lane UI consumes rows that satisfy it. A deviation goes
 * through the orchestrator, never lane-to-lane.
 *
 * Spec: docs/plans/show-annotation-message-type.md
 * Examples: docs/plans/show-annotation-message-type/examples.json
 */

/** Who authored the annotation. Selects the card title, nothing else. */
export type ShowAnnotationDirection = 'user' | 'agent'

/**
 * What happened to the annotation. `resolved` is the only value that renders a
 * visible state element; the others render nothing beyond the body itself.
 */
export type ShowAnnotationAction = 'created' | 'updated' | 'resolved' | 'dismissed'

/**
 * `content.annotation` — the display record. Every field here is rendered.
 * Anything the card does not draw belongs in `metadata`, not here.
 */
export interface ShowAnnotationDisplay {
  direction: ShowAnnotationDirection
  action: ShowAnnotationAction
  /**
   * Copy the reader can find on the page, condensed to at most 60 characters
   * with a trailing ellipsis. Absent when the anchor carries no human-readable
   * copy — a locator the user cannot read is noise, not a locator.
   */
  quote?: string
}

/**
 * The chat row. Field names and nesting are exactly what
 * `GET /api/sessions/<id>/messages` returns (see `WorkbenchMessage` in
 * `ui/src/context/ApiContext.tsx`); only the annotation-relevant fields are
 * narrowed here.
 */
export interface ShowAnnotationMessageRow {
  id: string
  /** The single gate on transcript visibility. */
  type: 'annotation'
  /** `harness` when the row is turn input, `user` for a non-dispatching human
   * annotation event, `agent` for a reverse mark. Never consulted for
   * visibility, and never consulted to pick the card. */
  author: 'harness' | 'user' | 'agent'
  source: 'harness' | null
  author_name: 'show_annotation' | null
  /** Human words only: the user's comment, or the agent's `--message`. May be
   * empty, in which case the card renders title + quote + attachments alone. */
  text: string
  content: {
    /** Mirrors `text`. */
    text: string
    annotation: ShowAnnotationDisplay
    /** The materialized screenshot, in the same record shape a Web chat upload
     * writes, so the existing attachment renderer shows a thumbnail. Absent
     * when the annotation carried no screenshot. */
    attachments?: Array<Record<string, unknown>>
  }
  /**
   * Machine facts. None of these are rendered in chat. `_queued_dispatch_text`
   * is present from the moment the row is reserved and holds the agent-facing
   * prompt; it is stripped when the row flushes.
   */
  metadata: {
    source: 'show_page'
    show_event_id: string
    show_event_type: string
    show_event_scope: string
    _queued_dispatch_text?: string
    [key: string]: unknown
  }
  created_at: string
}
