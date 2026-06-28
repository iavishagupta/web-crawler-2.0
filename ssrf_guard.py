import ipaddress
import socket
import urllib.parse as parser
from typing import Optional 

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("10.0.0.0/8"),         # private class A
    ipaddress.ip_network("172.16.0.0/12"),      # private class B
    ipaddress.ip_network("192.168.0.0/16"),     # private class C
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / metadata
    ipaddress.ip_network("0.0.0.0/8"),          # unspecified
    ipaddress.ip_network("100.64.0.0/10"),      # shared address space (RFC 6598)
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

_ALLOWED_SCHEMES = ["http", "https"]

class SSRFError(Exception):
    """Raised when URL is blocked for SSRF reasons."""

def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any([addr in net for net in _BLOCKED_NETWORKS])
    except ValueError:
        return True #block unparsable IP to be safe
    
def _resolve_host(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
        return [info[4][0] for info in infos]
    except socket.gaierror:
        return[]

def validate_url_safe(url: str) -> None:
    parsed = parser.urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(
          f"Blocked scheme '{parsed.scheme}' in URL: {url!r}. "
          f"Only {_ALLOWED_SCHEMES} are allowed."  
        )
    
    hostname = parsed.hostname
    if not hostname:
        raise SSRFError(f"URL has no hostname: {url!r}")
    
    if parsed.username or parsed.password:
        raise SSRFError(
            f"URL contains embedded credentials, which are not allowed: {url!r}"
        )
    
    try:
        ipaddress.ip_address(hostname)
        is_bare_ip = True
    except ValueError:
        is_bare_ip = False

    if is_bare_ip and _is_private_ip(hostname):
        raise SSRFError(
        f"URL resolves to a private/internal IP address ({hostname}): {url!r}"
    )

    if not is_bare_ip:
        resolved_ips = _resolve_host(hostname)
        if not resolved_ips:
            raise SSRFError(
                f"Hostname '{hostname}' could not be resolved: {url!r}"
            )
        for ip in resolved_ips:
            if _is_private_ip(ip):
                raise SSRFError(
                    f"Hostname '{hostname}' resolves to private IP {ip}: {url!r}"
                )


#-----------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    safe_urls = [
        "https://example.com/page",
        "http://boot.dev/learn",
    ]
    blocked_urls = [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://192.168.1.1/router",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ]
 
    print("=== Safe URLs (should pass) ===")
    for u in safe_urls:
        try:
            validate_url_safe(u)
            print(f"  PASS  {u}")
        except SSRFError as e:
            print(f"  FAIL  {u}  →  {e}")
 
    print("\n=== Blocked URLs (should raise) ===")
    for u in blocked_urls:
        try:
            validate_url_safe(u)
            print(f"  MISS  {u}  ← should have been blocked!")
        except SSRFError as e:
            print(f"  BLOCKED  {u}")
            print(f"           {e}")

    