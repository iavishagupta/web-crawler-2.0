import hashlib
import unittest

import sys
sys.path.insert(0, '/home/claude/queue_crawler')

from extract_html import (
    extract_page_data,
    _get_title,
    _get_headings,
    _get_body_text,
    _get_meta,
    _get_json_ld,
    _get_links,
    _get_images,
    _content_hash,
)
from bs4 import BeautifulSoup


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


BASE = "https://example.com"


# ── Title ─────────────────────────────────────────────────────────────────────

class TestGetTitle(unittest.TestCase):
    def test_extracts_title(self):
        s = soup("<html><head><title>Hello World</title></head></html>")
        self.assertEqual(_get_title(s), "Hello World")

    def test_strips_whitespace(self):
        s = soup("<title>  Padded  </title>")
        self.assertEqual(_get_title(s), "Padded")

    def test_no_title_returns_empty(self):
        s = soup("<html><body><p>No title</p></body></html>")
        self.assertEqual(_get_title(s), "")


# ── Headings ──────────────────────────────────────────────────────────────────

class TestGetHeadings(unittest.TestCase):
    def test_extracts_all_levels(self):
        s = soup("<h1>One</h1><h2>Two</h2><h3>Three</h3>")
        result = _get_headings(s)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], {"level": "h1", "text": "One"})
        self.assertEqual(result[1], {"level": "h2", "text": "Two"})
        self.assertEqual(result[2], {"level": "h3", "text": "Three"})

    def test_multiple_h1(self):
        s = soup("<h1>First</h1><h1>Second</h1>")
        result = _get_headings(s)
        self.assertEqual(len(result), 2)

    def test_skips_empty_headings(self):
        s = soup("<h1>   </h1><h2>Real</h2>")
        result = _get_headings(s)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "Real")

    def test_no_headings_returns_empty_list(self):
        s = soup("<p>No headings here</p>")
        self.assertEqual(_get_headings(s), [])

    def test_heading_with_nested_tags(self):
        s = soup("<h1><span>Nested</span> Text</h1>")
        result = _get_headings(s)
        self.assertIn("Nested", result[0]["text"])
        self.assertIn("Text", result[0]["text"])


# ── Body text ─────────────────────────────────────────────────────────────────

class TestGetBodyText(unittest.TestCase):
    def test_extracts_visible_text(self):
        s = soup("<body><p>Hello world</p></body>")
        self.assertEqual(_get_body_text(s), "Hello world")

    def test_excludes_script(self):
        s = soup("<body><script>var x = 1;</script><p>Visible</p></body>")
        result = _get_body_text(s)
        self.assertNotIn("var x", result)
        self.assertIn("Visible", result)

    def test_excludes_style(self):
        s = soup("<body><style>body{color:red}</style><p>Text</p></body>")
        result = _get_body_text(s)
        self.assertNotIn("color", result)
        self.assertIn("Text", result)

    def test_excludes_nav(self):
        s = soup("<body><nav>Menu items</nav><main><p>Content</p></main></body>")
        result = _get_body_text(s)
        self.assertNotIn("Menu items", result)
        self.assertIn("Content", result)

    def test_excludes_header_and_footer(self):
        s = soup("<body><header>Top</header><p>Middle</p><footer>Bottom</footer></body>")
        result = _get_body_text(s)
        self.assertNotIn("Top", result)
        self.assertNotIn("Bottom", result)
        self.assertIn("Middle", result)

    def test_collapses_whitespace(self):
        s = soup("<body><p>Too   many   spaces</p></body>")
        result = _get_body_text(s)
        self.assertNotIn("  ", result)

    def test_empty_body_returns_empty(self):
        s = soup("<body></body>")
        self.assertEqual(_get_body_text(s), "")


# ── Content hash ──────────────────────────────────────────────────────────────

class TestContentHash(unittest.TestCase):
    def test_returns_sha256_hex(self):
        result = _content_hash("hello")
        expected = hashlib.sha256("hello".encode()).hexdigest()
        self.assertEqual(result, expected)

    def test_same_text_same_hash(self):
        self.assertEqual(_content_hash("abc"), _content_hash("abc"))

    def test_different_text_different_hash(self):
        self.assertNotEqual(_content_hash("abc"), _content_hash("xyz"))

    def test_hash_length(self):
        self.assertEqual(len(_content_hash("test")), 64)


# ── Meta ──────────────────────────────────────────────────────────────────────

class TestGetMeta(unittest.TestCase):
    def _meta(self, html):
        return _get_meta(soup(html))

    def test_description(self):
        m = self._meta('<meta name="description" content="A test page">')
        self.assertEqual(m["description"], "A test page")

    def test_keywords(self):
        m = self._meta('<meta name="keywords" content="python, crawling">')
        self.assertEqual(m["keywords"], "python, crawling")

    def test_canonical(self):
        m = self._meta('<link rel="canonical" href="https://example.com/page">')
        self.assertEqual(m["canonical"], "https://example.com/page")

    def test_robots(self):
        m = self._meta('<meta name="robots" content="noindex,nofollow">')
        self.assertEqual(m["robots"], "noindex,nofollow")

    def test_og_tags(self):
        html = '''
            <meta property="og:title" content="OG Title">
            <meta property="og:description" content="OG Desc">
            <meta property="og:image" content="https://example.com/img.jpg">
        '''
        m = self._meta(html)
        self.assertEqual(m["og"]["og:title"], "OG Title")
        self.assertEqual(m["og"]["og:description"], "OG Desc")
        self.assertEqual(m["og"]["og:image"], "https://example.com/img.jpg")

    def test_twitter_tags(self):
        html = '''
            <meta name="twitter:card" content="summary_large_image">
            <meta name="twitter:title" content="TW Title">
        '''
        m = self._meta(html)
        self.assertEqual(m["twitter"]["twitter:card"], "summary_large_image")
        self.assertEqual(m["twitter"]["twitter:title"], "TW Title")

    def test_language_from_html_tag(self):
        m = self._meta('<html lang="en-US"><body></body></html>')
        self.assertEqual(m["language"], "en-US")

    def test_missing_fields_return_empty(self):
        m = self._meta("<html><body></body></html>")
        self.assertEqual(m["description"], "")
        self.assertEqual(m["canonical"], "")
        self.assertEqual(m["og"], {})
        self.assertEqual(m["twitter"], {})
        self.assertEqual(m["language"], "")


# ── JSON-LD ───────────────────────────────────────────────────────────────────

class TestGetJsonLd(unittest.TestCase):
    def _ld(self, html):
        return _get_json_ld(soup(html))

    def test_single_object(self):
        html = '''<script type="application/ld+json">
            {"@type": "Article", "headline": "Test"}
        </script>'''
        result = self._ld(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["@type"], "Article")

    def test_array_of_objects(self):
        html = '''<script type="application/ld+json">
            [{"@type": "Product"}, {"@type": "BreadcrumbList"}]
        </script>'''
        result = self._ld(html)
        self.assertEqual(len(result), 2)

    def test_multiple_scripts(self):
        html = '''
            <script type="application/ld+json">{"@type": "Article"}</script>
            <script type="application/ld+json">{"@type": "Organization"}</script>
        '''
        result = self._ld(html)
        self.assertEqual(len(result), 2)

    def test_malformed_json_skipped(self):
        html = '''<script type="application/ld+json">{ INVALID JSON }</script>'''
        result = self._ld(html)
        self.assertEqual(result, [])

    def test_no_json_ld_returns_empty(self):
        result = self._ld("<html><body></body></html>")
        self.assertEqual(result, [])

    def test_empty_script_skipped(self):
        html = '<script type="application/ld+json">   </script>'
        result = self._ld(html)
        self.assertEqual(result, [])


# ── Links ─────────────────────────────────────────────────────────────────────

class TestGetLinks(unittest.TestCase):
    def _links(self, html, base=BASE):
        return _get_links(soup(html), base)

    def test_absolute_link(self):
        links = self._links('<a href="https://example.com/about">About</a>')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], "https://example.com/about")

    def test_relative_link_resolved(self):
        links = self._links('<a href="/about">About</a>')
        self.assertEqual(links[0]["url"], "https://example.com/about")

    def test_anchor_text(self):
        links = self._links('<a href="/page">Click here</a>')
        self.assertEqual(links[0]["anchor_text"], "Click here")

    def test_title_attribute(self):
        links = self._links('<a href="/page" title="Page title">Link</a>')
        self.assertEqual(links[0]["title"], "Page title")

    def test_rel_attribute(self):
        links = self._links('<a href="https://other.com" rel="nofollow">Link</a>')
        self.assertIn("nofollow", links[0]["rel"])

    def test_internal_link_classified(self):
        links = self._links('<a href="/about">About</a>')
        self.assertFalse(links[0]["is_external"])

    def test_external_link_classified(self):
        links = self._links('<a href="https://other.com/page">External</a>')
        self.assertTrue(links[0]["is_external"])

    def test_skips_mailto(self):
        links = self._links('<a href="mailto:test@example.com">Email</a>')
        self.assertEqual(links, [])

    def test_skips_tel(self):
        links = self._links('<a href="tel:+1234567890">Call</a>')
        self.assertEqual(links, [])

    def test_skips_javascript(self):
        links = self._links('<a href="javascript:void(0)">Click</a>')
        self.assertEqual(links, [])

    def test_skips_anchor_only(self):
        links = self._links('<a href="#section">Jump</a>')
        self.assertEqual(links, [])

    def test_deduplicates_same_url(self):
        html = '<a href="/page">Link 1</a><a href="/page">Link 2</a>'
        links = self._links(html)
        self.assertEqual(len(links), 1)

    def test_no_links_returns_empty(self):
        links = self._links("<p>No links here</p>")
        self.assertEqual(links, [])

    def test_internal_and_external_split(self):
        html = '''
            <a href="/internal">Internal</a>
            <a href="https://other.com">External</a>
        '''
        links = self._links(html)
        internal = [l for l in links if not l["is_external"]]
        external = [l for l in links if l["is_external"]]
        self.assertEqual(len(internal), 1)
        self.assertEqual(len(external), 1)


# ── Images ────────────────────────────────────────────────────────────────────

class TestGetImages(unittest.TestCase):
    def _imgs(self, html, base=BASE):
        return _get_images(soup(html), base)

    def test_absolute_src(self):
        imgs = self._imgs('<img src="https://example.com/logo.png" alt="Logo">')
        self.assertEqual(imgs[0]["url"], "https://example.com/logo.png")

    def test_relative_src_resolved(self):
        imgs = self._imgs('<img src="/logo.png" alt="Logo">')
        self.assertEqual(imgs[0]["url"], "https://example.com/logo.png")

    def test_alt_text(self):
        imgs = self._imgs('<img src="/img.jpg" alt="Alt text">')
        self.assertEqual(imgs[0]["alt"], "Alt text")

    def test_dimensions(self):
        imgs = self._imgs('<img src="/img.jpg" width="400" height="300">')
        self.assertEqual(imgs[0]["width"], "400")
        self.assertEqual(imgs[0]["height"], "300")

    def test_missing_dimensions_are_none(self):
        imgs = self._imgs('<img src="/img.jpg" alt="No dims">')
        self.assertIsNone(imgs[0]["width"])
        self.assertIsNone(imgs[0]["height"])

    def test_skips_data_uri(self):
        imgs = self._imgs('<img src="data:image/png;base64,abc123" alt="inline">')
        self.assertEqual(imgs, [])

    def test_deduplicates_same_src(self):
        html = '<img src="/logo.png"><img src="/logo.png">'
        imgs = self._imgs(html)
        self.assertEqual(len(imgs), 1)

    def test_no_images_returns_empty(self):
        imgs = self._imgs("<p>No images</p>")
        self.assertEqual(imgs, [])


# ── Full extract_page_data ────────────────────────────────────────────────────

class TestExtractPageData(unittest.TestCase):
    FULL_HTML = '''<html lang="en">
    <head>
        <title>Test Page</title>
        <meta name="description" content="A test">
        <meta name="keywords" content="test, crawl">
        <meta property="og:title" content="OG Test">
        <meta name="twitter:card" content="summary">
        <link rel="canonical" href="https://example.com/test">
        <script type="application/ld+json">
            {"@type": "WebPage", "name": "Test"}
        </script>
    </head>
    <body>
        <header>Site header</header>
        <nav>Navigation</nav>
        <h1>Main Heading</h1>
        <h2>Subheading</h2>
        <p>First paragraph with real content.</p>
        <a href="/about" title="About us">About</a>
        <a href="https://external.com" rel="nofollow">External</a>
        <a href="mailto:hi@example.com">Email</a>
        <img src="/logo.png" alt="Logo" width="200" height="100">
        <img src="data:image/png;base64,abc" alt="inline">
        <footer>Footer content</footer>
    </body>
    </html>'''

    def setUp(self):
        self.result = extract_page_data(self.FULL_HTML, "https://example.com/test")

    def test_all_top_level_keys_present(self):
        expected_keys = {
            "url", "crawled_at", "title", "headings", "body_text",
            "word_count", "content_hash", "meta", "json_ld",
            "outgoing_links", "internal_links", "external_links", "images",
        }
        self.assertEqual(set(self.result.keys()), expected_keys)

    def test_url(self):
        self.assertEqual(self.result["url"], "https://example.com/test")

    def test_crawled_at_is_iso(self):
        from datetime import datetime
        # Should parse without error
        datetime.fromisoformat(self.result["crawled_at"])

    def test_title(self):
        self.assertEqual(self.result["title"], "Test Page")

    def test_headings(self):
        levels = [h["level"] for h in self.result["headings"]]
        self.assertIn("h1", levels)
        self.assertIn("h2", levels)

    def test_body_text_excludes_nav_header_footer(self):
        body = self.result["body_text"]
        self.assertNotIn("Site header", body)
        self.assertNotIn("Navigation", body)
        self.assertNotIn("Footer content", body)
        self.assertIn("First paragraph", body)

    def test_word_count_positive(self):
        self.assertGreater(self.result["word_count"], 0)

    def test_content_hash_is_sha256(self):
        self.assertEqual(len(self.result["content_hash"]), 64)

    def test_meta_fields(self):
        meta = self.result["meta"]
        self.assertEqual(meta["description"], "A test")
        self.assertEqual(meta["keywords"], "test, crawl")
        self.assertEqual(meta["canonical"], "https://example.com/test")
        self.assertEqual(meta["og"]["og:title"], "OG Test")
        self.assertEqual(meta["twitter"]["twitter:card"], "summary")
        self.assertEqual(meta["language"], "en")

    def test_json_ld(self):
        self.assertEqual(len(self.result["json_ld"]), 1)
        self.assertEqual(self.result["json_ld"][0]["@type"], "WebPage")

    def test_internal_links(self):
        self.assertIn("https://example.com/about", self.result["internal_links"])

    def test_external_links(self):
        self.assertIn("https://external.com", self.result["external_links"])

    def test_mailto_not_in_links(self):
        all_urls = [l["url"] for l in self.result["outgoing_links"]]
        self.assertFalse(any("mailto" in u for u in all_urls))

    def test_images_excludes_data_uri(self):
        img_urls = [i["url"] for i in self.result["images"]]
        self.assertFalse(any("data:" in u for u in img_urls))
        self.assertIn("https://example.com/logo.png", img_urls)

    def test_image_dimensions(self):
        logo = next(i for i in self.result["images"] if "logo" in i["url"])
        self.assertEqual(logo["width"], "200")
        self.assertEqual(logo["height"], "100")

    def test_same_content_same_hash(self):
        result2 = extract_page_data(self.FULL_HTML, "https://example.com/other")
        self.assertEqual(self.result["content_hash"], result2["content_hash"])

    def test_different_content_different_hash(self):
        other_html = self.FULL_HTML.replace("First paragraph", "Different content")
        result2 = extract_page_data(other_html, "https://example.com/test")
        self.assertNotEqual(self.result["content_hash"], result2["content_hash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)