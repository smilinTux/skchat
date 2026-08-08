"""A derived device label has to tell two of the operator's devices APART.

The whole point of Linked Devices is answering "which of my devices is this?".
The first live cutover produced two rows both labelled plainly "Chrome": a phone
and a Linux desktop, indistinguishable, which is the exact failure the epic set
out to fix (it was only ever described for native clients collapsing to "Dart").

The signed-label path cannot rescue the common case: the Flutter WEB build has no
``dart:io``, so it correctly sends no label and every browser device falls back to
this server-side derivation. That makes the derivation the only thing standing
between the operator and an unreadable list, so it has to carry the OS as well as
the browser.

Ordering is the whole trick, and every case below exists because a naive
substring check gets it wrong:
  * an Android UA also contains "Linux"
  * an iOS UA also contains "like Mac OS X"
  * a Chrome UA also contains "Safari"
  * an Edge UA also contains both "Chrome" and "Safari"
"""

from __future__ import annotations

import pytest

from skchat.operator_auth_routes import _derive_label

# Real User-Agent strings, including the two the live cutover actually produced.
ANDROID_CHROME = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
LINUX_CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
IPHONE_SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
MAC_SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
WINDOWS_EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
LINUX_FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
DART_NATIVE = "Dart/3.5 (dart:io)"


def test_the_two_devices_from_the_live_cutover_are_distinguishable():
    """The regression this file exists for: both used to be plain "Chrome"."""
    phone, _ = _derive_label(ANDROID_CHROME)
    desktop, _ = _derive_label(LINUX_CHROME)
    assert phone != desktop, "a phone and a desktop must not share a label"
    assert phone == "Chrome on Android"
    assert desktop == "Chrome on Linux"


@pytest.mark.parametrize(
    "ua,expected_label,expected_platform",
    [
        (ANDROID_CHROME, "Chrome on Android", "android"),
        (LINUX_CHROME, "Chrome on Linux", "linux"),
        (IPHONE_SAFARI, "Safari on iOS", "ios"),
        (MAC_SAFARI, "Safari on macOS", "macos"),
        (WINDOWS_EDGE, "Edge on Windows", "windows"),
        (LINUX_FIREFOX, "Firefox on Linux", "linux"),
    ],
)
def test_browser_and_os_are_both_recovered(ua, expected_label, expected_platform):
    assert _derive_label(ua) == (expected_label, expected_platform)


def test_android_beats_linux_because_its_ua_contains_both():
    assert "Linux" in ANDROID_CHROME
    assert _derive_label(ANDROID_CHROME)[1] == "android"


def test_ios_beats_macos_because_its_ua_says_like_mac_os_x():
    assert "Mac OS X" in IPHONE_SAFARI
    assert _derive_label(IPHONE_SAFARI)[1] == "ios"


def test_chrome_beats_safari_and_edge_beats_chrome():
    assert "Safari" in LINUX_CHROME
    assert _derive_label(LINUX_CHROME)[0].startswith("Chrome")
    assert "Chrome" in WINDOWS_EDGE
    assert _derive_label(WINDOWS_EDGE)[0].startswith("Edge")


def test_a_native_dart_client_is_named_without_a_bogus_os():
    """A native client sends no browser and no OS in its UA, so do not invent one.

    R2 exists precisely because this case cannot be derived usefully: the native
    app is expected to send its OWN signed label.
    """
    label, platform = _derive_label(DART_NATIVE)
    assert label == "App device"
    assert platform == "app"
    assert " on " not in label


def test_an_unknown_browser_on_a_known_os_still_names_the_os():
    label, platform = _derive_label("Mozilla/5.0 (Windows NT 10.0; Win64; x64) SomeNewBrowser/1.0")
    assert platform == "windows"
    assert "Windows" in label


def test_an_empty_user_agent_degrades_without_raising():
    assert _derive_label("") == ("Unknown device", "unknown")
    assert _derive_label(None) == ("Unknown device", "unknown")


def test_a_totally_unrecognised_agent_is_truncated_not_dropped():
    label, platform = _derive_label("x" * 200)
    assert platform == "unknown"
    assert 0 < len(label) <= 40
