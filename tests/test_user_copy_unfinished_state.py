"""User-visible copy must not expose internal stages, roadmaps or unfinished work.

A sentence such as "Phase 1 doesn't support X yet" or "coming soon" tells the
user the product is half-built and promises a plan nobody committed to. A real
limitation is written as a fact the user can act on ("Remote instances don't
support Mini. Return to this Mac to use it.") or not shown at all.

The scan covers every shipped string catalog: the Web/Desktop/Mini UI bundles,
the service and IM reply bundles, and the iOS catalogs. A legitimate match must
be listed in ``ALLOWED`` with the reason it is not roadmap or unfinished-state
copy; keep that list short and specific.

Scope: this guard blocks a fixed vocabulary of stage, roadmap and
unfinished-state phrases (Phase N, coming soon, "(soon)", 即将上线, 未来的,
暂不支持, "not supported yet", ...). It deliberately does not chase every
natural-language paraphrase: "yet", 还没 and 尚未 are everywhere in legitimate
empty-state and transient-state copy ("No projects yet", "Can't confirm yet"),
so matching them would trade a short allowlist for a long one. Paraphrases are
covered by the AGENTS.md copy rule and by review, not by growing this pattern.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

JSON_CATALOGS = (
    "ui/src/i18n/en.json",
    "ui/src/i18n/zh.json",
    "vibe/i18n/en.json",
    "vibe/i18n/zh.json",
)
XCSTRINGS_CATALOGS = (
    "ios/Avibe/Resources/Localizable.xcstrings",
    "ios/Avibe/Resources/InfoPlist.xcstrings",
    "ios/Avibe/Resources/AppShortcuts.xcstrings",
)
STRINGS_CATALOGS = ("ios/NotificationService/Localizable.strings",)

FORBIDDEN = re.compile(
    "|".join(
        (
            # Development stages and roadmap.
            r"第\s*[一二三四五六七八九十\d]+\s*(?:阶段|期)",
            r"首期|下一阶段|下个阶段|后续版本|后续\s*commit|以后会支持",
            # A feature promised for later, as a parenthetical or a "future" noun.
            r"未来(?:的|版本|会|将)|将来(?:的|会|将)|日后(?:会|将)",
            r"\((?:soon|coming soon|later|planned|upcoming)\)|\bin (?:the|a) future\b",
            r"即将(?:推出|上线|开放|支持|就位)|敬请期待",
            r"\bphase\s*(?:\d+|one|two|three)\b",
            r"\bcoming soon\b|\bstay tuned\b|\bstill to come\b|\bon the way\b",
            r"\b(?:later|future|upcoming|next)\s+(?:release|version|commit|phase)s?\b",
            r"\broadmap\b|\bMVP\b|\bWIP\b|\bTODO\b|\bTBD\b",
            # Unfinished state.
            r"还在路上|还在做|搭建中|开发中|施工中|还在迁移",
            r"暂不支持|暂未支持|尚未支持|尚未实现|暂未开放|预览版暂",
            r"\bunder (?:construction|development)\b|\bwork in progress\b",
            r"\bbeing (?:built|migrated)\b|\bredesign queue\b|\bvery early preview\b",
            r"\b(?:supported|implemented)\s+yet\b",
            r"\b(?:doesn['’]t|does not|don['’]t|do not|not)\s+support\b[^.。]{0,40}\byet\b",
            r"\bnot supported in the preview\b",
        )
    ),
    re.IGNORECASE,
)

# "<catalog>:<key>" -> why the match may stay for now. Every entry is a
# tracked exception, not a precedent: rewrite the copy and delete the entry.
_DEFERRED = "owner 2026-11-16 deferred; not in Desktop/iOS scope"
ALLOWED: dict[str, str] = {
    "ui/src/i18n/en.json:vaults.dialog.solanaImportHint": _DEFERRED,
    "ui/src/i18n/zh.json:vaults.dialog.solanaImportHint": _DEFERRED,
    "ui/src/i18n/en.json:vaults.askOff.legacyRegistration": _DEFERRED,
    "ui/src/i18n/en.json:vaults.hd.protectedUnsupported": _DEFERRED,
    "ui/src/i18n/zh.json:vaults.hd.protectedUnsupported": _DEFERRED,
    "ui/src/i18n/zh.json:vaults.reveal.errors.unavailable": _DEFERRED,
    "vibe/i18n/en.json:vault.signing.protectedSolanaUnsupported": _DEFERRED,
    "vibe/i18n/zh.json:vault.signing.protectedSolanaUnsupported": _DEFERRED,
    "vibe/i18n/zh.json:vault.signing.policyDowngradeUnavailable": _DEFERRED,
    "vibe/i18n/zh.json:vault.policy.context_unsupported": _DEFERRED,
    "vibe/i18n/en.json:info.genericFooter": _DEFERRED,
    "vibe/i18n/zh.json:info.genericFooter": _DEFERRED,
    "vibe/i18n/en.json:command.setup.unsupportedBackend": _DEFERRED,
    "vibe/i18n/zh.json:command.setup.unsupportedBackend": _DEFERRED,
}


def _walk(value: object, prefix: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{prefix}[{index}]")
    elif isinstance(value, str):
        yield prefix, value


def _entries() -> Iterator[tuple[str, str]]:
    for rel in JSON_CATALOGS:
        payload = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        for key, text in _walk(payload):
            yield f"{rel}:{key}", text
    for rel in XCSTRINGS_CATALOGS:
        payload = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        for key, entry in payload.get("strings", {}).items():
            for locale, localization in entry.get("localizations", {}).items():
                for path, text in _walk(localization):
                    yield f"{rel}:{key}[{locale}]{path}", text
    for rel in STRINGS_CATALOGS:
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            match = re.match(r'\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', line)
            if match:
                yield f"{rel}:{match.group(1)}", match.group(2)


def test_user_copy_has_no_unfinished_or_roadmap_wording() -> None:
    hits = [
        f"{location}: {text}" for location, text in _entries() if location not in ALLOWED and FORBIDDEN.search(text)
    ]
    assert hits == [], (
        "User-visible copy exposes an internal stage, roadmap or unfinished state. "
        "Write the limitation as a fact with a next step, or remove it:\n" + "\n".join(hits)
    )


def test_allowlist_entries_still_match() -> None:
    # A rewritten entry must leave the list, so the exception cannot quietly
    # cover a later regression of the same key. A deleted key is harmless and
    # is not failed here, so a parallel PR that removes the copy cannot break
    # the default branch; its leftover entry is cleaned up with the next edit.
    present = dict(_entries())
    stale = sorted(key for key in ALLOWED if key in present and not FORBIDDEN.search(present[key]))
    assert stale == [], f"remove ALLOWED entries whose copy was rewritten: {stale}"


def test_pattern_catches_known_unfinished_copy() -> None:
    for sample in (
        "第一阶段暂不支持远程实例的 Mini 和原生通知。",
        "Mini is unavailable for remote instances in Phase 1.",
        "This feature is coming soon!",
        "终端将在第二阶段上线",
        "This capability is still on the way — the full surface lands in a later release.",
        "Recovery phrases aren’t supported yet.",
        "Backend `x` does not support setup yet.",
        "Scheduled tasks, watches, and (soon) webhooks all come from a chat.",
        "后台任务 / 等待器 / 未来的 Webhook 都通过和 Agent 对话创建。",
    ):
        assert FORBIDDEN.search(sample), sample
    for sample in (
        "No projects yet",
        "Not fetched yet",
        "暂时连不上 avibe.bot。检查网络后重试。",
        "暂无会话",
        "Computer Use on Windows needs an x64 PC.",
        "The service has not confirmed Computer Use support yet.",
        "迁移中…",
        "Hear as soon as work is done or needs you.",
        "请先设置新的未来时间，再恢复它。",
    ):
        assert not FORBIDDEN.search(sample), sample
