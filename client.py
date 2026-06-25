import requests

def get_html(url: str):
    try:
        res = requests.get(url, headers={"User-Agent": "BootCrawler/1.0"})
        
        # HTTP errors (4xx, 5xx)
        if res.status_code >= 400:
            raise Exception(f"Failed to retrieve HTML. Status code: {res.status_code}")
        
        # content-type header (default to empty string if missing)
        content_type = res.headers.get("content-type", "").lower()
        
        # 'text/html' is present in the header (handles 'text/html; charset=utf-8')
        if "text/html" not in content_type:
            raise Exception(f"Returned content type is not HTML: {content_type}")
            
    except requests.RequestException as e:
        # catch network-related errors
        raise Exception(f"Error requesting the URL: {e}")
    
    return res.text