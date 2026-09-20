from core.system_prompt_injection import build_system_prompt_blocks


def _memory_prompt(*, memory_enabled: bool, profile_enabled: bool = True) -> str:
    return "\n".join(
        block.text
        for block in build_system_prompt_blocks(
            memory_enabled=memory_enabled,
            profile_enabled=profile_enabled,
        )
        if block.module_id == "memory-context-prompt"
    )


def test_profile_off_omits_only_profile_command_and_preserves_memory_guidance():
    off = _memory_prompt(memory_enabled=True, profile_enabled=False)
    on = _memory_prompt(memory_enabled=True, profile_enabled=True)
    assert "vibe memory profile" not in off
    for text in ("vibe memory search", "vibe memory status", "vibe memory remember", "safety"):
        assert text in off.lower()
    assert "vibe memory profile" in on


def test_memory_disabled_does_not_render_memory_prompt():
    blocks = build_system_prompt_blocks(memory_enabled=False, profile_enabled=False)
    assert all(block.module_id != "memory-context-prompt" for block in blocks)
