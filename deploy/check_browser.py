#!/usr/bin/env python3
"""Launch the scheduled browser and verify LearnUs media codec support."""

from __future__ import annotations

import os

from playwright.sync_api import sync_playwright


def main() -> int:
    channel = os.environ.get("YONSTUDY_BROWSER_CHANNEL", "chrome").strip() or "chrome"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=channel,
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        try:
            page = browser.new_page()
            support = page.evaluate(
                "() => document.createElement('video').canPlayType("
                "'video/mp4; codecs=\"avc1.42E01E, mp4a.40.2\"')"
            )
            if not support:
                raise RuntimeError(
                    f"{channel} does not support the H.264/AAC media required by LearnUs"
                )
        finally:
            browser.close()
    print(f"browser={channel} h264_aac=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
