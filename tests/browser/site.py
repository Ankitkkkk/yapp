"""Static-site browser QA; requires Playwright and Chromium, no app server.

Run: python tests/browser/site.py [--artifacts /tmp/yapp-site-screens]
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
INSTALL = 'curl -fsSL https://yapp.riggedcode.com/install.sh | sh'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check(browser, base, artifacts):
    errors = []
    context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
    # Published pages must remain usable without external network services.
    context.route('https://**/*', lambda route: route.abort())
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: errors.append(response.url) if response.status >= 400 else None)
    try:
        page.goto(base)
        page.get_by_role('button', name='Copy install command', exact=True).click()
        assert page.evaluate('navigator.clipboard.readText()') == INSTALL
        assert page.get_by_role('status').count() == 1, 'Copy feedback must be announced'
        assert page.get_by_role('status').inner_text()
        # Clipboard permission denial must still let the user copy the command.
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw Error('denied')}}})")
        page.get_by_role('button', name='Copy install command', exact=True).click()
        assert page.evaluate('window.getSelection().toString()') == INSTALL
        assert 'selected' in page.get_by_role('status').inner_text().lower()

        # The illustration must switch both visual content and accessible state.
        toggle = page.locator('[data-markdown-toggle]')
        assert toggle.inner_text() == 'F7 · Show raw Markdown'
        assert page.locator('[data-markdown-formatted]').is_visible()
        toggle.click()
        assert toggle.get_attribute('aria-pressed') == 'true'
        assert toggle.inner_text() == 'F7 · Show formatted text'
        assert not page.locator('[data-markdown-formatted]').is_visible()
        assert '```python' in page.locator('[data-markdown-raw]').inner_text()
        assert page.locator('[data-markdown-raw]').is_visible()
        toggle.press('Enter')
        assert toggle.get_attribute('aria-pressed') == 'false'
        assert page.locator('[data-markdown-formatted]').is_visible()
        assert not page.locator('[data-markdown-raw]').is_visible()

        for width in (320, 360, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 960})
            for path in ('/', '/features/', '/docs/', '/404.html'):
                response = page.goto(base + path)
                assert response.status == 200, path
                assert page.locator('h1').count() == 1, path
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, path)
                # Every local navigation target must exist in the published site.
                for href in page.locator('a[href]').evaluate_all('(links) => links.map(a => a.getAttribute("href"))'):
                    if href.startswith('#'):
                        assert page.locator(href).count(), href
                    elif href.startswith('/'):
                        result = context.request.get(base + href.split('#')[0])
                        assert result.status == 200, href
                if artifacts:
                    name = 'home' if path == '/' else path.strip('/').replace('.', '-')
                    page.screenshot(path=str(artifacts / f'{name}-{width}.png'), full_page=True)

        page.goto(base)
        page.keyboard.press('Tab')
        assert page.locator(':focus').get_attribute('href') == '#main', 'Skip link must be first'
        page.keyboard.press('Enter')
        assert page.locator(':focus').get_attribute('id') == 'main'
        page.locator('details summary').first.click()
        assert page.locator('details[open]').count()
        page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        for path in ('/', '/features/', '/docs/', '/404.html'):
            page.goto(base + path)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), path
            if artifacts:
                name = 'home' if path == '/' else path.strip('/').replace('.', '-')
                page.screenshot(path=str(artifacts / f'{name}-dark.png'), full_page=True)
        assert not errors, errors
    finally:
        context.close()

    context = browser.new_context(java_script_enabled=False, viewport={'width': 360, 'height': 800})
    context.route('https://**/*', lambda route: route.abort())
    try:
        page = context.new_page()
        page.goto(base)
        assert page.get_by_text(INSTALL, exact=True).count()
        assert page.locator('[data-markdown-formatted]').is_visible()
        assert not page.locator('[data-markdown-toggle]').is_visible()
        page.get_by_role('link', name='Features', exact=True).first.click()
        assert page.url.endswith('/features/')
        assert page.locator('#terminal').inner_text()
        page.get_by_role('link', name='Setup guide', exact=True).first.click()
        assert page.url.endswith('/docs/')
        assert page.locator('main').inner_text()
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    if args.artifacts:
        args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='yapp-site-check-') as directory:
        staging = Path(directory) / 'site'
        shutil.copytree(ROOT / 'site', staging)
        for name in ('install.sh', 'llms.txt'):
            shutil.copy2(ROOT / name, staging / name)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(staging)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    check(browser, f'http://127.0.0.1:{server.server_port}', args.artifacts)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print('PASS: clipboard, Markdown demo, keyboard, responsive layouts, local links, dark mode, and no-JS navigation')


if __name__ == '__main__':
    main()
