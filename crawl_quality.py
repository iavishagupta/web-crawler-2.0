from typing import Optional
import urllib.parse as parser
import asyncio

from logger import get_logger
log = get_logger(__name__)

def resolve_canonical(
        fetched_url: str,           #URL we actually fetched
        canonical_raw: str,         #The raw href from <link rel="canonical">
        base_domain: str,
) -> Optional[str]:   
    canonical_raw = canonical_raw.strip()
    if canonical_raw is None:
        return None
    
    canonical = parser.urlparse.urljoin(fetched_url, canonical_raw)
    
    canonical_parsed = parser.urlparse(canonical)
    fetched_parsed = parser.urlparse(fetched_url)

    if canonical_parsed.scheme not in ("http", "https"):
        return None
    
    if canonical_parsed.netloc != base_domain:
        log.debug(
            "canonical_cross_domain_ignored",
            fetched_url=fetched_url,
            canonical=canonical,
            base_domain=base_domain,
        )
        return None
    
    if canonical == fetched_url:
        return None
    
    return canonical

class CanonicalResolver:
    def __init__(self, base_domain: str):
        self.base_domain = base_domain
        
    def resolve(self, fetched_url: str, canonical_raw: str) -> Optional[str]:
        canonical = resolve_canonical(fetched_url, canonical_raw, self.base_domain)
        if canonical is None:
            return None
        
        from url_normalizer import normalize_url
        fetched_normalized=normalize_url(fetched_url)
        canonical_normalized=normalize_url(canonical)

        if canonical_normalized == fetched_normalized:
            return None
        
        log.debug(
            "canonical_resolved",
            fetched_url=fetched_url,
            canonical=canonical_normalized,
        )

        return canonical_normalized
    
class ContentDeduplicator:
    def __init__(self):
        self._seen: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def chech_and_register(
            self,
            content_hash: Optional[str],
            url: str
    ) -> tuple[bool, Optional[str]]:        #returns (is_duplicate, original_url)
        if not content_hash:
            return False, None
        
        async with self._lock:
            if content_hash in self._seen:
                return True, self._seen[content_hash]
            
            self._seen[content_hash] = url 
            return False, None 
        
    def stats(self) -> dict:
        return {
            "unique_content_hashes": len(self._seen),
        }