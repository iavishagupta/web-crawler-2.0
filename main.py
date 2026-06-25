import asyncio
import sys, validators #type: ignore
from client import get_html
from crawl import crawl_page
from async_crawler import crawl_site_async
from json_report import write_json_report

def validate_cli_cmd_and_extract_URL(cli_input: str) :
    if len(cli_input) < 2:
        print('no website provided')
        sys.exit(1)

    BASE_URL = cli_input[1]
    if len(cli_input) == 2:
        return BASE_URL, 0, 0

    if len(cli_input) == 3:
        max_concurrecy = cli_input[2] 
        return BASE_URL, max_concurrecy, 0

    if len(cli_input) == 4:
        max_concurrecy = cli_input[2] 
        max_pages = cli_input[3]
        return BASE_URL, max_concurrecy, max_pages
    
    if len(cli_input) > 4:
        print('too many inputs provided')
        sys.exit(1)        

    if not is_valid_url(BASE_URL):
        print("invalid URL provided")
        sys.exit(1)

def is_valid_url(url_string):
    result = validators.url(url_string)
    return isinstance(result, bool) and result

async def main():
    cli_input = sys.argv
    BASE_URL, max_concurrency, max_pages = validate_cli_cmd_and_extract_URL(cli_input)

    # html_docs = get_html(BASE_URL)
    # print(html_docs)

    # pages = crawl_page(BASE_URL)
    pages = await crawl_site_async(BASE_URL, int(max_concurrency), int(max_pages))
    write_json_report(pages)
    
if __name__ == "__main__":
    asyncio.run(main())
