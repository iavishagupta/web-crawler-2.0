import os
import aiohttp, asyncio #type:ignore
from typing import Optional
import urllib.parse as parser


from ssrf_guard import validate_url_safe, SSRFError

from dotenv import load_dotenv #type: ignore
load_dotenv()

MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

FETCH_TIMEOUT = aiohttp.ClientTimeout(
    total=30,
    connect=10,
    sock_read=15,
)
USER_AGENT = os.getenv("USER_AGENT")

class RedirectError(Exception):
    """Raised when a redirect violates our policy."""

async def get_html_with_guard(
        session: aiohttp.ClientSession,
        url: str,
        base_domain: Optional[str] = None,
        allow_cross_domain: bool = False,
        user_agent: str = USER_AGENT,
) -> tuple[str, str]:
    try:
        safe_ip_url, original_host = validate_url_safe(url)
    except SSRFError as e:
        raise RedirectError(f"URL blocked by SSRF Guard: {e}") from e
    
    current_url = safe_ip_url
    redirect_chain: list[str] = [url]
    headers = {"User-Agent":user_agent, "Host":original_host}

    logical_url = url
    for hop in range (MAX_REDIRECTS + 1):
        async with session.get(
            current_url,
            headers=headers,
            allow_redirects=False,
            timeout=FETCH_TIMEOUT,
        ) as res:
            
            if res.status not in REDIRECT_STATUSES:
                if res.status >= 400:
                    raise aiohttp.ClientResponseError(
                        res.request_info,
                        res.history,
                        status=res.status,
                        message=f"HTTP {res.status}",
                    )
                
                content_type = res.headers.get("content-type", "").lower()
                if "text/html" not in content_type:
                    raise ValueError(
                        f"Non-HTML content type '{content_type}' at '{current_url}'"
                    )
                
                html = await res.text()
                return html, logical_url
            
            if hop == MAX_REDIRECTS:
                raise RedirectError(
                    f"Redirect chain exceeded {MAX_REDIRECTS} hops."
                    f"Chain: {' -> '.join(redirect_chain)}"
                )
            
            location = res.headers.get("Location")
            if not location:
                raise RedirectError(
                    f"Redirect response {res.status} from {current_url} "
                    f"has no Location header."
                )
            
            next_url = parser.urljoin(current_url, location)

            try:
                safe_next_url, next_host = validate_url_safe(next_url)
            except SSRFError as e:
                raise RedirectError(f"Redirect target blocked by SSRF Guard: {e}") from e

            redirect_chain.append(next_url)
            current_url = safe_next_url
            logical_url = next_url
            headers = {"User-Agent": user_agent, "Host": next_host}
            
            if not allow_cross_domain and base_domain:
                next_domain = parser.urlparse(next_url).netloc
                if next_domain != base_domain:
                    raise RedirectError(
                        f"Redirect crossed domain boundary: "
                        f"{base_domain} → {next_domain} "
                        f"(redirected to {next_url!r})"
                    )
                
    raise RedirectError("Redirect loop terminated unexpectedly.")