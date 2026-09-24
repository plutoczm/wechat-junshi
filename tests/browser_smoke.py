"""Offline DOM + ASGI smoke test. No browser navigation or real API calls.

CSP, auth and HTTP boundaries are tested separately in test_mobile.py.
Clipboard and UUID are test doubles: this is NOT a real-device TLS/clipboard test.
Run: python tests/browser_smoke.py [--screenshot /tmp/mobile.png]
"""
import argparse
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from mobile.app import create_app


class FakeModel:
    key = "synthetic-only"

    def generate(self, snapshot):
        return ["\u597d\u5440\uff0c\u6211\u4eec\u63d0\u524d\u5546\u91cf"], "\u6f14\u793a\u8349\u7a3f\uff0c\u672a\u8c03\u7528\u771f\u5b9e\u6a21\u578b\u3002"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    token = "dom-test-token-" + "x" * 32
    static = Path(__file__).resolve().parents[1] / "mobile" / "static"
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / "test.sqlite3", token, "http://testserver", FakeModel())
        store = app.state.store
        person = store.add_person("\u4eba\u7269 A\uff08\u6f14\u793a\uff09")
        account = store.add_account(person, "A-\u5927\u53f7")
        alt = store.add_account(person, "A-\u5c0f\u53f7")
        store.configure(account, True)
        store.configure(alt, True)
        store.add_note(alt, "\u4e0d\u559c\u6b22\u4e34\u65f6\u6539\u7ea6\uff08\u5408\u6210\u6d4b\u8bd5\u6570\u636e\uff09")
        with TestClient(app) as client, sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=shutil.which("chromium"))
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            def request(data):
                assert data["path"].startswith("/api/")
                r = client.request(data["method"], data["path"], headers=data["headers"], content=data.get("body"))
                return {"status": r.status_code, "body": r.text}
            page.expose_function("localRequest", request)
            page.expose_function("localUUID", lambda: str(uuid.uuid4()))
            html = re.sub(r'<link[^>]*>|<script.*?</script>', '', (static / "index.html").read_text(), flags=re.S)
            page.set_content(html)
            page.add_style_tag(content=(static / "style.css").read_text())
            page.evaluate('''() => {
                window.fetch = async (path, options={}) => {
                    const r = await window.localRequest({path, method: options.method || 'GET', headers: options.headers || {}, body: options.body});
                    return new Response(r.body, {status:r.status,headers:{'Content-Type':'application/json'}});
                };
                Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.testClipboard = text;}}});
                Object.defineProperty(crypto, 'randomUUID', {value: () => 'dom-' + Math.random().toString(36).slice(2)});
            }''')
            page.add_script_tag(content=(static / "app.js").read_text())
            page.locator("#token").fill(token)
            page.locator("#connect").click()
            expect(page.locator("#workspace")).to_be_visible()
            page.locator(".account-button").first.click()
            expect(page.locator("#accountPanel")).to_be_visible()
            page.wait_for_function("!busy")
            page.locator("#incoming").fill("\u5468\u672b\u8981\u4e0d\u8981\u89c1\u9762\uff1f")
            page.locator("#generate").click()
            expect(page.locator("#draftPanel")).to_be_visible()
            page.wait_for_function("!busy")
            assert len(store.export(account)) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#copy").click()
            page.wait_for_function("!busy")
            assert page.evaluate("window.testClipboard") == "\u597d\u5440\uff0c\u6211\u4eec\u63d0\u524d\u5546\u91cf"
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            page.locator("#draftText").fill("Edited actual message")
            page.locator("#sent").click()
            expect(page.locator("#draftPanel")).to_be_hidden()
            page.wait_for_function("!busy")
            assert store.export(account)[-1]["content"] == "Edited actual message"
            store.add_note(account, "<img src=x onerror=alert(1)>")
            page.locator("#searchBtn").click()
            page.wait_for_function("!busy")
            assert "<img" in page.locator("#notes").text_content()
            assert page.locator("#notes img").count() == 0
            page.set_viewport_size({"width": 1280, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            browser.close()
    print("Offline DOM/ASGI smoke passed: phone/desktop layout, C workflow, edited confirmation, XSS-safe rendering.")


if __name__ == "__main__":
    main()
