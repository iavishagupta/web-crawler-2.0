from bs4 import BeautifulSoup, Tag #type: ignore
import urllib.parse as parser
from typing import TypedDict

def get_heading_from_html(html :str) -> str :
    soup = BeautifulSoup(html, 'html.parser')

    h1 = soup.h1
    if h1 :
        return h1.text
    else : 
        h2 = soup.h2
        if h2 :
            return h2.text
        else :
            return ""
        
def get_first_paragraph_from_html(html: str) -> str :
    soup = BeautifulSoup(html, 'html.parser')

    try:
        main_in_body = soup.main
        if main_in_body:
            return main_in_body.p.text
    except Exception:
        print('Main function not found, using fallback way.')

    try :
        first_para = soup.p
        if first_para:
            return first_para.text
    except Exception:
        print("Paragraph also not found, returning null string")
        return ""
    
def get_urls_from_html(html: str, base_url: str):
    soup = BeautifulSoup(html, 'html.parser')
    unnormalized_link_list = []

    link_tags = soup.find_all('a')
    
    if link_tags == []:
        return 'No Link Found'
    
    for link_tag in link_tags:
        link = link_tag.get('href')

        parsed_link = parser.urlparse(link)
        if parsed_link.netloc == '':
            link = parser.urljoin(base_url, parsed_link.path)

        unnormalized_link_list.append(link)
    return unnormalized_link_list

def get_images_from_html(html: str, base_url: str):
    soup = BeautifulSoup(html, 'html.parser')
    unnormalized_link_list = []

    link_tags = soup.find_all('img')
    
    if link_tags == []:
        return 'No Image Found'
    
    for link_tag in link_tags:
        link = link_tag.get('src')

        parsed_link = parser.urlparse(link)
        if parsed_link.netloc == '':
            link = parser.urljoin(base_url, parsed_link.path)

        unnormalized_link_list.append(link)
    return unnormalized_link_list
    
class PageData(TypedDict):
    heading: str
    first_paragraph: str
    outgoing_links: list[str]
    image_urls: list[str]

def extract_page_data(html: str, page_url: str):
    pagedata = PageData()
    pagedata['url'] = page_url
    pagedata['heading'] = get_heading_from_html(html=html)
    pagedata['first_paragraph'] = get_first_paragraph_from_html(html=html)
    pagedata['outgoing_links'] = get_urls_from_html(html=html,base_url=page_url)
    pagedata['image_urls'] = get_images_from_html(html=html,base_url=page_url)

    return pagedata