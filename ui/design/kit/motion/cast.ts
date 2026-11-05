// What a motion demo is told about the data it stages: which Session each state reads, which rows are new,
// and how far the layout moves between states. The frames own the data and state these; the kit only moves.

/** The conversation's states, as Session ids, and the rows a moment brings in. */
export type ChatCast = {
  /** A draft typed and ready to send. */
  before: string;
  /** Sent: the message is in, the agent is working. */
  after: string;
  /** The reply has landed. */
  replied: string;
  sentId: string;
  replyId: string;
};

/** How far the conversation moves when rows go in below it (the row heights the shipped page draws for
 *  the data; FLIP measures them at run time): `send` the message and the working bubble, `arrival` what
 *  the reply adds over the bubble. */
export type ChatShift = { send: number; arrival: number };

/** The Inbox move: the card that moves to the top, and where each card of the new list sat in the old one,
 *  relative to its new slot (the FLIP offsets). */
export type InboxCast = { moved: string; flip: Record<string, number> };
