"""Dependency-free checks for the static GitHub Pages artifact.

Run: python3 tests/check_site.py
Checks crawlable content, metadata, JSON-LD, sitemap coverage, and local targets.
Browser layout and behavior are checked separately by tests/browser/site.py.
"""
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'site'
BASE = 'https://yapp.riggedcode.com/'
COPIED_ASSETS = {'/install.sh': ROOT / 'install.sh', '/llms.txt': ROOT / 'llms.txt'}


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__(convert_charrefs=True)
        self.path = path
        relative = path.relative_to(SITE).as_posix()
        self.url = BASE + relative.removesuffix('index.html')
        self.elements = []
        self.title = ''
        self.schemas = []
        self._title = False
        self._schema = None
        self.feed(path.read_text())
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == 'title':
            self._title = True
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self._schema = ''

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self._title:
            self.title += data
        if self._schema is not None:
            self._schema += data

    def handle_endtag(self, tag):
        if tag == 'title':
            self._title = False
        if tag == 'script' and self._schema is not None:
            self.schemas.append(json.loads(self._schema))
            self._schema = None

    def tags(self, name):
        return [attrs for tag, attrs in self.elements if tag == name]

    def meta(self, key):
        values = [a.get('content') for a in self.tags('meta')
                  if a.get('name', a.get('property')) == key]
        assert len(values) == 1 and values[0], (self.path, key, values)
        return values[0]


def main():
    pages = [Page(path) for path in sorted(SITE.rglob('*.html'))]
    by_url = {page.url: page for page in pages}
    assert len({page.title for page in pages}) == len(pages), 'Duplicate titles'
    assert len({page.meta('description') for page in pages}) == len(pages), 'Duplicate descriptions'
    indexable = set()
    target_count = 0
    for page in pages:
        assert len(page.tags('h1')) == len(page.tags('main')) == 1, page.path
        assert len(page.tags('title')) == 1 and page.title.strip(), page.path
        assert page.tags('html')[0].get('lang') == 'en', page.path
        assert 'width=device-width' in page.meta('viewport'), page.path
        ids = [a['id'] for _, a in page.elements if 'id' in a]
        assert all(count == 1 for count in Counter(ids).values()), (page.path, 'duplicate IDs')
        for key in ('title', 'description'):
            expected = page.title if key == 'title' else page.meta(key)
            assert page.meta('og:' + key) == page.meta('twitter:' + key) == expected, page.path
        assert page.meta('og:url') == page.url, page.path
        assert page.meta('og:image') == page.meta('twitter:image'), page.path
        assert page.meta('og:image:alt') == page.meta('twitter:image:alt'), page.path
        assert urlsplit(page.meta('og:image')).scheme == 'https', page.path
        if page.path.name == '404.html':
            assert 'noindex' in page.meta('robots'), page.path
        else:
            assert 'noindex' not in page.meta('robots'), page.path
            canonicals = [a['href'] for a in page.tags('link') if a.get('rel') == 'canonical']
            assert canonicals == [page.url], (page.path, canonicals)
            indexable.add(page.url)
            assert page.schemas, (page.path, 'missing structured data')
            graph = [item for schema in page.schemas for item in schema.get('@graph', [])]
            assert any(item.get('url') == page.url and item.get('@type') == 'WebPage'
                       for item in graph), page.path
            for schema in page.schemas:
                assert schema.get('@context') == 'https://schema.org', page.path
        for img in page.tags('img'):
            assert all(key in img for key in ('alt', 'width', 'height')), (page.path, img)
        targets = [a[key] for tag, a in page.elements for key in ('href', 'src') if key in a]
        targets += [page.meta('og:image')]
        for target in targets:
            parsed = urlsplit(urljoin(page.url, target))
            if parsed.netloc != urlsplit(BASE).netloc:
                continue
            path = unquote(parsed.path)
            asset = COPIED_ASSETS.get(path, SITE / path.lstrip('/'))
            if asset.is_dir():
                asset /= 'index.html'
            assert asset.is_file(), (page.path, 'missing target', target)
            if parsed.fragment and asset.suffix == '.html':
                linked = by_url[urljoin(BASE, path)]
                assert any(a.get('id') == unquote(parsed.fragment) for _, a in linked.elements), (page.path, target)
            target_count += 1
    namespace = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    entries = ET.parse(SITE / 'sitemap.xml').findall('s:url', namespace)
    urls = [entry.findtext('s:loc', namespaces=namespace) for entry in entries]
    assert len(urls) == len(set(urls)) and set(urls) == indexable, ('sitemap mismatch', urls, indexable)
    assert 'Sitemap: ' + BASE + 'sitemap.xml' in (SITE / 'robots.txt').read_text()
    for css in (SITE / 'assets').glob('*.css'):
        for target in re.findall(r'url\([\'\"]?([^\'\"\)]+)', css.read_text()):
            assert (SITE / target.lstrip('/')).is_file(), (css, target)
    print(f'PASS: {len(pages)} pages, {target_count} local targets, metadata, JSON-LD, and sitemap')


if __name__ == '__main__':
    main()
