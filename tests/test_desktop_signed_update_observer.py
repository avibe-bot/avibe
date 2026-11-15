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


def test_owned_child_is_reaped_before_relaunch(observer):
    from unittest.mock import Mock
    child = Mock()
    observer.OWNED_CHILDREN[123] = child
    child.poll.return_value = None
    assert observer.source_is_running(123)
    child.poll.return_value = 0
    assert not observer.source_is_running(123)
    assert child.poll.call_count == 2


def test_powershell_preserves_env_and_fails_closed(observer):
    import subprocess
    from unittest.mock import Mock
    result = Mock(returncode=0, stdout='{"Status": 0}\n', stderr='')
    with patch.object(observer.subprocess, 'run', return_value=result) as call:
        assert observer.powershell('Get-AuthenticodeSignature $env:SMOKE_INSTALLER', env={'SMOKE_INSTALLER': 'fixture'}) == '{"Status": 0}'
        assert call.call_args.args[0][:4] == ['pwsh.exe', '-NoProfile', '-NonInteractive', '-File']
        assert call.call_args.kwargs['env'] == {'SMOKE_INSTALLER': 'fixture'}
        assert "$ErrorActionPreference = 'Stop'" in (observer.ROOT / 'observer-command.ps1').read_text()
    failed = subprocess.CompletedProcess(['pwsh.exe'], 1, '', 'test error')
    with patch.object(observer.subprocess, 'run', return_value=failed), pytest.raises(subprocess.CalledProcessError):
        observer.powershell('throw')


def test_first_run_dismisses_offer_then_completes_no_key_setup(observer):
    from unittest.mock import Mock
    labels = ['Later', 'Get started', 'Set up later', observer.SOURCE_SHORT]
    phase = [0]
    clicked = []
    window = Mock()
    window.window_text.side_effect = lambda: 'Avibe Desktop Updates' if phase[0] == 0 else 'Avibe'
    app = Mock()
    app.top_window.return_value = window
    def button(_window, label, **_kwargs):
        if label != labels[phase[0]]:
            return None
        value = Mock()
        def click():
            clicked.append(label)
            phase[0] += 1
        value.click_input.side_effect = click
        return value
    def poll(callback, *_args):
        for _ in range(5):
            if callback():
                return True
        raise AssertionError('first-run flow did not finish')
    with patch.object(observer, 'windows_button', side_effect=button), patch.object(observer, 'windows_tree', return_value=[]), patch.object(observer, 'wait_for', side_effect=poll):
        observer.windows_first_run(app)
    assert clicked == ['Later', 'Get started', 'Set up later']
    assert all(a['action'] != 'click-update' for a in observer.ACTIONS)


def test_mac_version_requires_exact_field(observer):
    assert observer.mac_has_version('AXStaticText\t\t3.2.7rc3\t', observer.TARGET_SHORT)
    assert not observer.mac_has_version('AXStaticText\t\t3.2.7rc30\t', observer.TARGET_SHORT)
    assert not observer.mac_has_version('AXStaticText\tRelease notes for 3.2.7rc3\t', observer.TARGET_SHORT)
