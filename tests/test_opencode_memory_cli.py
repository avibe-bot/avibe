from types import SimpleNamespace
from unittest.mock import Mock

from modules.agents.opencode.agent import _disable_memory_cli_access


def test_opencode_revokes_memory_cli_access_and_clears_payload_capability() -> None:
    context = SimpleNamespace(
        platform_specific={"memory_cli_capability": "must-not-reach-opencode"},
    )
    configure_access = Mock()
    controller = SimpleNamespace(configure_memory_cli_access=configure_access)

    _disable_memory_cli_access(controller, context)

    configure_access.assert_called_once_with(context, admitted=False)
    assert "memory_cli_capability" not in context.platform_specific
