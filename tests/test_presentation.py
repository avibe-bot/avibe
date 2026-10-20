"""One presentation decision per agent output (MESSAGE-DELIVERY-337).

``DefaultPresentationPolicy`` holds the rules the dispatcher used to apply inline, in
their order: ``level``, background visibility, the Web interim copy of long narration,
the catalog's Web Push flags, the target's tool-call delivery, the concise status line,
the channel's hidden types, an empty log line, a channel that cannot edit, and the
turn's progress style. The dispatcher executes the decision and records it on the
message it writes.
"""

from __future__ import annotations

import pytest

from core.presentation import (
    DefaultPresentationPolicy,
    ImDelivery,
    PresentationDecision,
    PresentationInput,
    interim_worthy,
)

LONG = "\n".join(f"line {n}" for n in range(5))


def _never(name):
    def read():
        raise AssertionError(f"the policy read the {name} it does not need")

    return read


def _input(message_type="result", **fields):
    values = {
        "row_type": message_type,
        "level": "normal",
        "platform": "slack",
        "background": False,
        "body": "text",
        "log_chunk": "text",
        "hidden": _never("hidden types"),
        "progress_style": _never("progress style"),
        **fields,
    }
    return PresentationInput(message_type=message_type, **values)


def _decision(**fields):
    values = {
        "policy": "default/1",
        "persisted": True,
        "transcript": False,
        "muted": False,
        "web_push": False,
        "outward": True,
        "im": ImDelivery.NONE,
        "reason": "",
        **fields,
    }
    return PresentationDecision(**values)


def _style(value):
    return lambda: value


@pytest.mark.parametrize(
    ("presentation", "expected"),
    [
        # level and an empty body
        pytest.param(
            _input("result", level="silent"),
            _decision(persisted=False, reason="content_free"),
            id="silent-level-shows-nothing",
        ),
        pytest.param(
            _input("assistant", body="  "),
            _decision(persisted=False, reason="content_free"),
            id="empty-body-shows-nothing",
        ),
        # answers and notices
        pytest.param(
            _input("result", platform="avibe"),
            _decision(transcript=True, web_push=True, im=ImDelivery.MESSAGE),
            id="web-reply-pushes",
        ),
        pytest.param(
            _input("result"),
            _decision(transcript=True, im=ImDelivery.MESSAGE),
            id="im-reply-is-a-message",
        ),
        pytest.param(
            _input("result", row_type="error", platform="avibe"),
            _decision(transcript=True, web_push=True, im=ImDelivery.MESSAGE),
            id="web-failure-pushes",
        ),
        pytest.param(
            _input("output", platform="avibe"),
            _decision(transcript=True, muted=True, im=ImDelivery.MESSAGE),
            id="released-output-type-is-muted-and-silent",
        ),
        pytest.param(
            _input("notify", platform="avibe"),
            _decision(transcript=True, im=ImDelivery.MESSAGE),
            id="notice-does-not-push",
        ),
        pytest.param(
            _input("notify", platform="avibe", metadata={"event": "backend_failure"}),
            _decision(transcript=True, web_push=True, im=ImDelivery.MESSAGE),
            id="backend-failure-notice-pushes",
        ),
        pytest.param(
            _input("result", platform="avibe", background=True),
            _decision(transcript=True, outward=False, reason="background"),
            id="background-reply-is-local-history",
        ),
        # narration's Web copy
        pytest.param(
            _input("assistant", platform="avibe", narration_body=LONG, log_editable=True, hidden=lambda: False,
                   progress_style=_style("verbose")),
            _decision(transcript=True, muted=True, im=ImDelivery.LOG),
            id="long-web-narration-keeps-a-muted-copy",
        ),
        pytest.param(
            _input("assistant", platform="avibe", narration_body="short", hidden=lambda: False,
                   progress_style=_style("verbose")),
            _decision(im=ImDelivery.LOG),
            id="short-narration-is-activity-only",
        ),
        pytest.param(
            _input("assistant", platform="avibe", level="process", narration_body=LONG, hidden=lambda: False,
                   progress_style=_style("verbose")),
            _decision(im=ImDelivery.LOG),
            id="process-narration-keeps-no-copy",
        ),
        pytest.param(
            _input("assistant", platform="avibe", background=True, narration_body=LONG),
            _decision(outward=False, reason="background"),
            id="background-narration-keeps-no-copy",
        ),
        # the process log
        pytest.param(
            _input("toolcall", tools_deliverable=False),
            _decision(reason="tools_unsupported"),
            id="tools-the-target-cannot-deliver",
        ),
        pytest.param(
            _input("toolcall", log_chunk="", progress_style=_style("concise")),
            _decision(im=ImDelivery.STATUS),
            id="concise-status-takes-narration-and-tools-even-hidden-or-empty",
        ),
        pytest.param(
            _input("system", hidden=lambda: True),
            _decision(reason="hidden"),
            id="hidden-type",
        ),
        pytest.param(
            _input("assistant", hidden=lambda: True, progress_style=_style("verbose")),
            _decision(reason="hidden"),
            id="hidden-narration-outside-concise",
        ),
        pytest.param(
            _input("system", log_chunk="", hidden=lambda: False),
            _decision(reason="empty"),
            id="empty-log-line",
        ),
        pytest.param(
            _input("assistant", log_editable=False, hidden=lambda: False, progress_style=_style("off")),
            _decision(im=ImDelivery.LOG),
            id="a-channel-that-cannot-edit-gets-every-line",
        ),
        pytest.param(
            _input("system", hidden=lambda: False, progress_style=_style("off")),
            _decision(reason="style_off"),
            id="style-off",
        ),
        pytest.param(
            _input("system", hidden=lambda: False, progress_style=_style("concise")),
            _decision(im=ImDelivery.STATUS),
            id="concise-system-line",
        ),
        pytest.param(
            _input("assistant", hidden=lambda: False, progress_style=_style("verbose")),
            _decision(im=ImDelivery.LOG),
            id="verbose-log",
        ),
    ],
)
def test_the_default_policy_decides_each_rule(presentation, expected):
    """MESSAGE-DELIVERY-337: each of the dispatcher's former inline rules, as one decision;
    the hidden types and the progress style are read only when a rule needs them."""
    assert DefaultPresentationPolicy().decide(presentation) == expected


@pytest.mark.parametrize(
    ("text", "worthy"),
    [
        ("one\ntwo\nthree\nfour", False),
        ("one\ntwo\nthree\nfour\nfive", True),
        ("x" * 499, False),
        ("x" * 500, True),
        ("字" * 250, True),
        ("字" * 249, False),
    ],
)
def test_the_interim_threshold_counts_lines_and_display_width(text, worthy):
    """MESSAGE-DELIVERY-337: five non-blank lines, or 500 columns with wide characters
    counted twice."""
    assert interim_worthy(text) is worthy


def test_a_decision_is_recorded_as_stable_metadata():
    """MESSAGE-DELIVERY-337: the record a message keeps names the policy and each decided field."""
    decision = _decision(transcript=True, web_push=True, im=ImDelivery.MESSAGE)
    assert decision.to_metadata() == {
        "policy": "default/1",
        "transcript": True,
        "muted": False,
        "web_push": True,
        "im": "message",
    }
    assert _decision(reason="hidden").to_metadata()["reason"] == "hidden"
