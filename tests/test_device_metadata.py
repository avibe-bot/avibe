"""What a device row is called before the client describes itself."""

import pytest

from vibe.device_metadata import describe


def _ua(value):
    return describe({"User-Agent": value})


@pytest.mark.parametrize(
    ("ua", "version"),
    [
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) AvibeDesktop/3.2.9-rc.1", "3.2.9-rc.1"),
        ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 AvibeDesktop/3.2.9", "3.2.9"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_1 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 AvibeiOS/3.2.8-beta.2+7", "3.2.8-beta.2+7"),
        ("Mozilla/5.0 (Macintosh) AvibeDesktop/3.2.9-rc.1(arm64)", "3.2.9-rc.1"),
    ],
)
def test_the_app_version_keeps_its_whole_pre_release_suffix(ua, version):
    assert _ua(ua)["app_version"] == version


def test_the_app_version_is_bounded():
    assert len(_ua("AvibeDesktop/3." + "1" * 200)["app_version"]) == 64


def test_an_unnamed_web_view_is_called_by_its_engine_and_is_not_a_browser():
    meta = _ua("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)")
    assert meta == dict(kind="unknown", display_name="WebKit · macOS", app_version=None, system_version=None)


def test_a_client_with_no_recognizable_marker_is_not_a_browser():
    assert _ua("curl/8.7.1") == dict(kind="unknown", display_name=None, app_version=None, system_version=None)
    assert _ua("")["kind"] == "unknown"


@pytest.mark.parametrize(
    ("ua", "name"),
    [
        ("Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "Chrome · macOS"),
        ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/26.1 Safari/605.1.15", "Safari · macOS"),
        ("Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 CriOS/131.0 Mobile/15E148 Safari/604.1", "Chrome · iOS"),
    ],
)
def test_a_recognized_browser_is_still_a_browser(ua, name):
    meta = _ua(ua)
    assert meta["kind"] == "browser" and meta["display_name"] == name
