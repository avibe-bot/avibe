"""What a notify delivery attempt actually proved, as one value.

Dependency-free on purpose: the message dispatcher fills it, the message mirror
contributes to it, and the owed-failure-notice drain branches on it, so it may not
import any of them.

Why a structured result rather than the message id alone. The notify branch's
contract is ``Optional[str]``, and ``None`` is ambiguous three ways:

* ``persist_agent_message`` swallows its own failures and returns ``None``, and the
  notify branch discarded that return value entirely;
* the branch's blanket ``except`` returned ``None`` for a send failure, a
  persistence failure and a post-delivery stream failure alike.

A drain that acked on "returned cleanly" would therefore mark a notice ``sent`` on
a delivery that never happened — losing it permanently, which is the exact silent
failure the durable notice exists to prevent. A drain that acked only on a
persisted row could not tell "never delivered" from "delivered, bookkeeping
failed", and would re-send a notice the user already has.

So all three signals travel: what was delivered, what was persisted, and what
went wrong where.

And an id is not uniformly evidence, which is why ``ack_evidence`` distinguishes
the two strengths rather than collapsing them into one boolean.
``AvibeBot.send_message`` mints and returns a synthetic ``msg_<hex>`` id
UNCONDITIONALLY — whether or not an SSE subscriber exists, and whether or not
anything was persisted — so on that platform ``delivery_only`` proves nothing and
only a receipt does. On a real IM platform the returned id came from the platform,
so it does prove the user was told. Who may ack on what is the CALLER's policy,
enumerated per target class in ``core.scheduled_tasks.LADDER_ACK_SOURCES`` and
applied by ``ScheduledTaskService._rung_acknowledges``; this module only reports
which of the two strengths it has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: A persisted ``messages`` row. The strongest evidence: the row implies the send
#: returned an id (or that the transport persists without delivering), and it is
#: what later dedupes the identity.
ACK_EVIDENCE_RECEIPT = "receipt"
#: The transport returned an id but the row write failed. On a real IM platform that
#: id came from the platform, so this is positive evidence the user was told,
#: recorded explicitly rather than pretending a receipt exists — re-sending because
#: a DB write failed would spam a notice that already arrived. On avibe the id is
#: synthetic and this proves nothing; see the module docstring.
ACK_EVIDENCE_DELIVERY_ONLY = "delivery_only"

#: Where in the notify branch an error came from. ``stream`` is the one that must
#: NOT read as a delivery failure: send and persist both succeeded and only the
#: live SSE fan-out raised, so resending would duplicate a delivered notice.
STAGE_SEND = "send"
STAGE_PERSIST = "persist"
STAGE_STREAM = "stream"


@dataclass
class DeliveryEvidence:
    """Filled in by the notify branch; read by whoever owes the notice."""

    delivered_id: Optional[str] = None
    persisted_row: Optional[dict[str, Any]] = None
    error: Optional[BaseException] = None
    error_stage: Optional[str] = None
    #: True once the send call returned without raising, whatever it returned —
    #: distinguishing "the transport refused" from "the transport accepted and told
    #: us nothing useful", which ``delivered_id`` alone cannot.
    send_returned: bool = field(default=False)

    @property
    def ack_evidence(self) -> Optional[str]:
        """How delivery was proved, or ``None`` when it was not."""

        if self.persisted_row is not None:
            return ACK_EVIDENCE_RECEIPT
        if self.delivered_id is not None:
            return ACK_EVIDENCE_DELIVERY_ONLY
        return None

    @property
    def delivered(self) -> bool:
        return self.ack_evidence is not None

    @property
    def error_text(self) -> Optional[str]:
        """The failure's own message, for the dead letter to report.

        The raised exception's text, never a generic string: a dead letter that
        cannot say why is an actionable-failure requirement reduced to "something
        went wrong".
        """

        if self.error is None:
            return None
        detail = str(self.error).strip() or type(self.error).__name__
        return f"{self.error_stage or 'delivery'}: {detail}"
