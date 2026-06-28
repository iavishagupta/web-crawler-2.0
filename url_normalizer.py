import urllib.parse as urlparse
from w3lib.url import canonicalize_url

TRACKING_PARAMS: frozenset[str] = frozenset({
    # Google Analytics
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_source_platform",

    # Google Ads
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid",

    # Meta / Facebook
    "fbclid", "fb_action_ids", "fb_action_types", "fb_source",
    "fb_ref", "fbaction",

    # Microsoft Ads
    "msclkid",

    # HubSpot
    "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src",
    "hsa_tgt", "hsa_kw", "hsa_mt", "hsa_net", "hsa_ver",
    "_hsenc", "_hsmi",

    # Mailchimp
    "mc_eid", "mc_cid",

    # Marketo
    "mkt_tok",

    # Twitter / X
    "twclid",

    # TikTok
    "tt_medium", "tt_content",

    # Yahoo
    "yclid",

    # Adobe Analytics
    "s_cid", "icid",

    # Generic click-tracking noise
    "trk", "trkCampaign", "sc_campaign", "sc_channel", "sc_content",
    "sc_medium", "sc_outcome", "sc_geo", "sc_country",
})


def normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication.

    Steps:
      1. canonicalize_url — lowercase scheme/host, sort params,
                            normalize encoding, strip fragments
      2. Strip tracking query params
      3. Remove trailing ? if all params were stripped

    Examples:
      normalize_url("HTTPS://Example.COM/post?utm_source=x&page=2")
      -> "https://example.com/post?page=2"

      normalize_url("https://example.com/post?utm_source=x")
      -> "https://example.com/post"

      normalize_url("https://example.com/post?id=42&fbclid=xyz")
      -> "https://example.com/post?id=42"
    """
    # Step 1: standard canonicalization
    url = canonicalize_url(url)

    # Step 2: strip tracking params
    parsed = urlparse.urlsplit(url)
    if not parsed.query:
        return url

    clean_params = [
        (k, v)
        for k, v in urlparse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]

    # Step 3: rebuild URL
    new_query = urlparse.urlencode(clean_params)
    return urlparse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        new_query,
        "",          # fragment always stripped (canonicalize_url already did this)
    ))


def get_domain(url: str) -> str:
    return urlparse.urlparse(url).netloc


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cases = [
        # Tracking-only params → stripped entirely
        ("https://example.com/post?utm_source=twitter&utm_campaign=spring",
         "https://example.com/post"),

        # Mixed: tracking stripped, real params kept
        ("https://example.com/post?utm_source=x&page=2&sort=price",
         "https://example.com/post?page=2&sort=price"),

        # fbclid stripped
        ("https://example.com/post?fbclid=abc123&id=42",
         "https://example.com/post?id=42"),

        # No params → unchanged
        ("https://example.com/post",
         "https://example.com/post"),

        # Uppercase host/scheme → lowercased
        ("HTTPS://Example.COM/post",
         "https://example.com/post"),

        # Fragment → stripped
        ("https://example.com/post#section",
         "https://example.com/post"),

        # Real params → kept
        ("https://example.com/search?q=python&page=3",
         "https://example.com/search?page=3&q=python"),  # sorted by canonicalize

        # All tracking, trailing ? removed
        ("https://example.com/post?utm_source=x&gclid=y",
         "https://example.com/post"),
    ]

    print("=== URL normalizer tests ===\n")
    all_pass = True
    for url, expected in cases:
        result = normalize_url(url)
        ok = result == expected
        all_pass = all_pass and ok
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {url[:55]:<55}")
        if not ok:
            print(f"        expected: {expected}")
            print(f"        got:      {result}")

    print(f"\n  {'All tests passed' if all_pass else 'FAILURES above'}")