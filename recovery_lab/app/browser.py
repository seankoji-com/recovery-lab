"""Run real Chromium inside the isolated recovery network."""
import json
import sys
import time

from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch()
    try:
        page = browser.new_page()
        expected = 503 if sys.argv[1] == "broken" else 200
        for attempt in range(30):
            try:
                response = page.goto("http://app:8080", timeout=3000)
                if response and response.status == expected:
                    break
            except Exception:
                pass
            if attempt == 29:
                raise RuntimeError("application readiness deadline exceeded")
            time.sleep(1)
        if expected == 200:
            page.get_by_role("link", name="The lighthouse log", exact=True).click()
            expect(page.get_by_role("heading", name="The lighthouse log", exact=True)).to_be_visible()
            expect(page.locator("article")).to_have_text(
                "The spare key is with the harbour keeper. Fixture record RL-001.")
        print(json.dumps({"engine": "chromium", "http_status": expected,
                          "journey": "broken source" if expected == 503 else "list -> note -> exact restored body",
                          "passed": True}))
    finally:
        browser.close()
