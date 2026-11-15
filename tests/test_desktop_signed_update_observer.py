"""Pure observer tests: no GUI, process launch, clipboard, keychain or networking."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def observer(tmp_path):
    path = Path(__file__).parents[1] / 'desktop/scripts/test-signed-rc-update-panel.py'
    spec = importlib.util.spec_from_file_location('signed_observer', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'SMOKE_ROOT': str(tmp_path)}):
        spec.loader.exec_module(module)
    return module


class Control:
    def __init__(self, text, kind='Button', enabled=True, top=0):
        self.text = text
        self.element_info = SimpleNamespace(control_type=kind)
        self.enabled = enabled
        self.top = top

    def window_text(self):
        return self.text

    def is_visible(self):
        return True

    def is_enabled(self):
        return self.enabled

    def rectangle(self):
        return SimpleNamespace(top=self.top)


class Window:
    def __init__(self, *controls):
        self.controls = controls

    def descendants(self):
        return self.controls


def test_display_version_matches_real_panel(observer):
    assert observer.SOURCE_SHORT == '3.2.7rc2'
    assert observer.TARGET_SHORT == '3.2.7rc3'
    assert observer.normalize_version(observer.TARGET_VERSION) == observer.normalize_version(observer.TARGET_SHORT)


def test_version_trigger_uses_button_not_child_text(observer):
    button = Control('v3.2.7rc2')
    window = Window(Control('3.2.7rc2', 'Text'), button)
    assert observer.windows_button(window, observer.SOURCE_SHORT, version=True) is button


def test_check_prefers_header_but_update_fails_ambiguous(observer):
    header = Control('Check for updates', top=1)
    lead = Control('Check for updates', top=100)
    assert observer.windows_button(Window(lead, header), 'Check for updates') is header
    with pytest.raises(RuntimeError, match='ambiguous'):
        observer.windows_button(Window(Control('Update'), Control('Update')), 'Update')
    assert observer.windows_button(Window(Control('Update', enabled=False)), 'Update') is None


def test_mac_version_requires_exact_field(observer):
    assert observer.mac_has_version('AXStaticText\t\t3.2.7rc3\t', observer.TARGET_SHORT)
    assert not observer.mac_has_version('AXStaticText\t\t3.2.7rc30\t', observer.TARGET_SHORT)
    assert not observer.mac_has_version('AXStaticText\tRelease notes for 3.2.7rc3\t', observer.TARGET_SHORT)
