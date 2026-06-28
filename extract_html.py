import hashlib
import json
import re
import urllib.parse as urlparse
from datetime import datetime, timezone
from typing import TypedDict, Optional

from bs4 import BeautifulSoup


#TypedDicts 

class LinkData(TypedDict):
    url: str
    anchor_text: str
    title: str
    rel: str             # nofollow, sponsored, ugc, etc.
    is_external: bool


class ImageData(TypedDict):
    url: str
    alt: str
    width: Optional[str]
    height: Optional[str]


class HeadingData(TypedDict):
    level: str           # "h1", "h2", ...
    text: str


class MetaData(TypedDict):
    description: str
    keywords: str
    canonical: str
    robots: str
    og: dict             # og:title, og:description, og:image, ...
    twitter: dict        # twitter:card, twitter:title, ...
    language: str


class PageData(TypedDict):
    url: str
    crawled_at: str

    title: str
    headings: list[HeadingData]
    body_text: str
    word_count: int
    content_hash: str

    meta: MetaData
    json_ld: list[dict]

    outgoing_links: list[LinkData]
    internal_links: list[str]
    external_links: list[str]

    images: list[ImageData]


#Extractors 

def _get_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    return tag.get_text(strip=True) if tag else ""


def _get_headings(soup: BeautifulSoup) -> list[HeadingData]:
    headings = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = tag.get_text(separator=" ", strip=True)
        if text:
            headings.append(HeadingData(level=tag.name, text=text))
    return headings


def _get_body_text(soup: BeautifulSoup) -> str:
    """
    Visible text only — excludes scripts, styles, nav, header, footer.
    This is roughly what a search engine indexes.
    """
    SKIP_TAGS = {"script", "style", "noscript", "header", "footer", "nav"}
    body = soup.find("body") or soup
    texts = []
    for element in body.descendants:
        if element.name in SKIP_TAGS:
            continue
        if hasattr(element, "parent") and element.parent and element.parent.name in SKIP_TAGS:
            continue
        if isinstance(element, str):
            text = element.strip()
            if text:
                texts.append(text)
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def _get_meta(soup: BeautifulSoup) -> MetaData:
    def meta_content(attr: str, val: str) -> str:
        tag = soup.find("meta", {attr: val})
        return tag.get("content", "").strip() if tag else ""

    og = {}
    for tag in soup.find_all("meta", property=re.compile(r"^og:")):
        k, v = tag.get("property", ""), tag.get("content", "")
        if k and v:
            og[k] = v

    twitter = {}
    for tag in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:")}):
        k, v = tag.get("name", ""), tag.get("content", "")
        if k and v:
            twitter[k] = v

    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""

    html_tag = soup.find("html")
    language = html_tag.get("lang", "").strip() if html_tag else ""

    return MetaData(
        description=meta_content("name", "description"),
        keywords=meta_content("name", "keywords"),
        canonical=canonical,
        robots=meta_content("name", "robots"),
        og=og,
        twitter=twitter,
        language=language,
    )


def _get_json_ld(soup: BeautifulSoup) -> list[dict]:
    """All JSON-LD structured data blocks. Skips malformed ones."""
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            text = script.string or ""
            if text.strip():
                data = json.loads(text)
                if isinstance(data, list):
                    results.extend(data)
                else:
                    results.append(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return results


def _get_links(soup: BeautifulSoup, base_url: str) -> list[LinkData]:
    """
    All <a href> links with anchor text, title, rel.
    Resolves relative URLs. Skips mailto:, tel:, javascript:, #anchors.
    """
    base_domain = urlparse.urlparse(base_url).netloc
    links = []
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        resolved = urlparse.urljoin(base_url, href)
        parsed = urlparse.urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)

        rel_attr = tag.get("rel", [])
        rel = " ".join(rel_attr) if isinstance(rel_attr, list) else str(rel_attr)

        links.append(LinkData(
            url=resolved,
            anchor_text=tag.get_text(separator=" ", strip=True),
            title=tag.get("title", "").strip(),
            rel=rel,
            is_external=parsed.netloc != base_domain,
        ))

    return links


def _get_images(soup: BeautifulSoup, base_url: str) -> list[ImageData]:
    """All <img src> with alt and dimensions. Skips data: URIs."""
    images = []
    seen = set()

    for tag in soup.find_all("img", src=True):
        src = tag.get("src", "").strip()
        if not src or src.startswith("data:"):
            continue
        resolved = urlparse.urljoin(base_url, src)
        if resolved in seen:
            continue
        seen.add(resolved)
        images.append(ImageData(
            url=resolved,
            alt=tag.get("alt", "").strip(),
            width=tag.get("width"),
            height=tag.get("height"),
        ))

    return images


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Main entry point 

def extract_page_data(html: str, page_url: str) -> PageData:
    """
    Parse HTML and return a fully-populated PageData.
    Single BeautifulSoup parse shared across all extractors.
    """
    soup = BeautifulSoup(html, "html.parser")
    body_text = _get_body_text(soup)
    links = _get_links(soup, page_url)

    return PageData(
        url=page_url,
        crawled_at=datetime.now(timezone.utc).isoformat(),

        title=_get_title(soup),
        headings=_get_headings(soup),
        body_text=body_text,
        word_count=len(body_text.split()) if body_text else 0,
        content_hash=_content_hash(body_text),

        meta=_get_meta(soup),
        json_ld=_get_json_ld(soup),

        outgoing_links=links,
        internal_links=[l["url"] for l in links if not l["is_external"]],
        external_links=[l["url"] for l in links if l["is_external"]],

        images=_get_images(soup, page_url),
    )