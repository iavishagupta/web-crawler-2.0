
import urllib.parse as parser
from typing import TypedDict
from extract_html import extract_page_data
from client import get_html

class PageData(TypedDict):
    heading: str
    first_paragraph: str
    outgoing_links: list[str]
    image_urls: list[str]

def normalize_url(url: str) -> str:
    parsed_url = parser.urlparse(url)

    return ''.join([parsed_url.netloc, parsed_url.path])

def get_domain(url: str) -> str:
    parsed = parser.urlparse(url=url)
    return parsed.netloc

def crawl_page(base_url, current_url=None, page_data=None):
    if current_url is None:
        current_url = base_url
    if page_data is None:
        page_data = {}

    if get_domain(current_url) != get_domain(base_url):
        return

    curr_url_norm = normalize_url(current_url)
    
    html = get_html(current_url)
    print(f"Crawling: {current_url}")
    
    extracted_data = extract_page_data(html, current_url)
    
    page_data[curr_url_norm] = extracted_data

    for link in extracted_data['outgoing_links'] :
        if normalize_url(link) in page_data:
            continue
        crawl_page(base_url, link, page_data)

    
    return page_data
    


    

    