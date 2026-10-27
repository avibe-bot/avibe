"""The background-work stubs in ``tests/conftest.py`` reach a module however late a test imports it.

``_no_background_web_push`` and ``_no_catalog_fetch`` keep the Web Push sender
and the catalog refreshes off threads that outlive their test. They no longer
import those modules for every test. A module that is not imported yet gets
the running test's stubs as its import finishes, and an imported module gets
them at setup as before. The inner run below imports all three modules for the
first time inside a test body, which is the path the conftest would leave
unstubbed if it applied stubs only at setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

_INNER = """
import sys

import pytest

WATCHED = ("core.web_push_notifications", "vibe.backend_model_catalog", "vibe.models_dev_catalog")


def test_a_module_first_imported_inside_a_test_is_stubbed(_no_background_web_push):
    assert not [name for name in WATCHED if name in sys.modules]

    from core import web_push_notifications
    from vibe import backend_model_catalog, models_dev_catalog

    web_push_notifications._send_to_enabled_subscriptions({"title": "held"})
    assert _no_background_web_push == [{"title": "held"}]
    assert backend_model_catalog.schedule_remote_catalog_refresh() is False
    assert models_dev_catalog._refresh_in_background() is False
    with pytest.raises(OSError, match="tests do not fetch models.dev"):
        models_dev_catalog._fetch_catalog({})


@pytest.mark.real_web_push_sender
@pytest.mark.real_catalog_refresh
def test_the_stubs_end_with_the_test_that_imported_the_module():
    from core import web_push_notifications
    from vibe import backend_model_catalog, models_dev_catalog

    for module, name in (
        (web_push_notifications, "_send_to_enabled_subscriptions"),
        (backend_model_catalog, "schedule_remote_catalog_refresh"),
        (models_dev_catalog, "_refresh_in_background"),
        (models_dev_catalog, "_fetch_catalog"),
    ):
        assert getattr(module, name).__module__ == module.__name__, name


def test_an_imported_module_is_stubbed_at_setup():
    from vibe import models_dev_catalog

    assert models_dev_catalog._refresh_in_background() is False
"""


def test_background_stubs_follow_a_late_first_import(pytester: pytest.Pytester, monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    pytester.makepyfile(test_inner=_INNER)
    result = pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider", "-p", "no:randomly")
    result.assert_outcomes(passed=3)
