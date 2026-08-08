#!/usr/bin/env python3
"""
shopify_scraper.py - an all-in-one, open-source Shopify store scraper.

Works with ANY Shopify-powered storefront (custom domains included) - it does
NOT work with non-Shopify websites. Everything a Shopify store exposes on its
public JSON endpoints can be pulled with a single command:

    python shopify_scraper.py https://gymshark.com

What it does
------------
  * Resolve any store URL (custom domain or *.myshopify.com) to its underlying
    myshopify.com domain and detect the store currency.
  * Confirm the site is really a Shopify store.
  * Download the FULL product catalogue via paginated /products.json.
  * Optionally download all collections (/collections.json) too.
  * Fetch a single product by handle, or a live variant price/stock via the
    lightweight /variants/{id}.js endpoint.
  * Export everything to JSON (raw) and/or CSV (one row per variant).

Zero required dependencies
--------------------------
Runs on the Python standard library alone. If any of these optional HTTP
clients are installed it will use them automatically for better success rates
against bot-protected stores (tried in this order):

    1. curl_cffi   - TLS fingerprint spoofing, best against Cloudflare
    2. tls_client  - alternative TLS impersonation
    3. httpx       - modern HTTP/2 client
    4. urllib      - always-available standard-library fallback

    pip install curl_cffi          # recommended
    pip install "curl_cffi httpx"  # even better coverage

Library usage
-------------
    from shopify_scraper import ShopifyScraper

    scraper = ShopifyScraper()
    result = scraper.scrape("https://gymshark.com", collections=True)
    print(result.store.myshopify_domain, result.product_count)
    result.save_json("gymshark.json")
    result.save_csv("gymshark.csv")

License: MIT.  This tool only reads public endpoints; scrape responsibly and
respect each store's terms of service and robots.txt.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

__version__ = "1.0.0"
__all__ = [
    "ShopifyScraper",
    "StoreInfo",
    "ScrapeResult",
    "NotAShopifyStoreError",
    "StoreBlockedError",
    "flatten_products",
]

logger = logging.getLogger("shopify_scraper")


# ---------------------------------------------------------------------------
# Browser-like headers - sent with every request so stores treat us as Chrome
# ---------------------------------------------------------------------------
BROWSER_HEADERS: dict[str, str] = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-US,en;q=0.9",
    "priority": "u=0, i",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

# Tweaked headers for JSON API calls (/products.json etc.)
JSON_HEADERS: dict[str, str] = {
    **BROWSER_HEADERS,
    "accept": "application/json, text/plain, */*",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# *.myshopify.com anywhere in a string
MYSHOPIFY_RE = re.compile(r"([\w-]+\.myshopify\.com)", re.IGNORECASE)

# The store's own canonical self-reference, e.g.
#   Shopify.shop = "brand.myshopify.com"      or
#   "myshopifyDomain":"brand.myshopify.com"
# Preferred over the first generic match, which could belong to an embedded app
# or a linked store elsewhere in the page.
MYSHOPIFY_CANONICAL_RE = re.compile(
    # The optional ["'] after the key matches the closing quote of the JSON
    # form  "myshopifyDomain":"brand.myshopify.com"  as well as the bare JS
    # assignment  Shopify.shop = "brand.myshopify.com".
    r'(?:myshopifyDomain|Shopify\.shop)["\']?\s*[=:]\s*["\']([\w-]+\.myshopify\.com)["\']',
    re.IGNORECASE,
)

# Shopify.currency = {"active":"GBP","rate":"1.0"};
CURRENCY_RE = re.compile(
    r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"',
    re.IGNORECASE,
)

# Best-effort store name from og:site_name or <title>
OG_SITE_NAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)

# HTTP statuses that make a proxy attempt worth retrying on the next proxy
_PROXY_TRIGGER_STATUSES: frozenset[int] = frozenset({401, 403, 407, 429, 500, 502, 503, 504})

# Statuses that mean the store is deliberately blocking us (password page,
# region lock, bot wall). Treated as a hard StoreBlockedError for products.
_BLOCKED_STATUSES: frozenset[int] = frozenset({401, 403})

# How many times to retry a single page on HTTP 429 before giving up, so a
# store that rate-limits every request can never loop forever.
_MAX_RATE_LIMIT_RETRIES = 5


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ShopifyScraperError(Exception):
    """Base class for all scraper errors."""


class NotAShopifyStoreError(ShopifyScraperError):
    """Raised when a URL cannot be resolved to a Shopify store."""


class StoreBlockedError(ShopifyScraperError):
    """Raised when a store actively blocks scraping (HTTP 403)."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class StoreInfo:
    """Everything we learn about a store while resolving it."""

    input_url: str
    domain: str                     # public host, e.g. "store.jeep.com"
    myshopify_domain: str           # e.g. "jeep-online-store.myshopify.com"
    currency: str = "USD"           # ISO-4217 code
    name: str | None = None         # best-effort human-readable store name

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_url": self.input_url,
            "domain": self.domain,
            "myshopify_domain": self.myshopify_domain,
            "currency": self.currency,
            "name": self.name,
        }


@dataclass
class ScrapeResult:
    """The complete result of scraping a store."""

    store: StoreInfo
    products: list[dict[str, Any]] = field(default_factory=list)
    collections: list[dict[str, Any]] = field(default_factory=list)
    scraped_at: str = ""

    @property
    def product_count(self) -> int:
        return len(self.products)

    @property
    def variant_count(self) -> int:
        return sum(len(p.get("variants", []) or []) for p in self.products)

    def to_dict(self) -> dict[str, Any]:
        return {
            "store": self.store.to_dict(),
            "scraped_at": self.scraped_at,
            "product_count": self.product_count,
            "variant_count": self.variant_count,
            "collection_count": len(self.collections),
            "products": self.products,
            "collections": self.collections,
        }

    def save_json(self, path: str, *, indent: int = 2) -> None:
        _ensure_parent_dir(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=indent, ensure_ascii=False)
        logger.info("Wrote JSON -> %s", path)

    def save_csv(self, path: str) -> None:
        rows = flatten_products(self.products, store=self.store)
        _write_csv(rows, path)
        logger.info("Wrote CSV (%d variant rows) -> %s", len(rows), path)


# ---------------------------------------------------------------------------
# CSV flattening
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "store_domain",
    "myshopify_domain",
    "product_id",
    "product_handle",
    "product_title",
    "vendor",
    "product_type",
    "tags",
    "published_at",
    "created_at",
    "updated_at",
    "variant_id",
    "variant_title",
    "sku",
    "price",
    "compare_at_price",
    "available",
    "requires_shipping",
    "taxable",
    "grams",
    "option1",
    "option2",
    "option3",
    "position",
    "image_src",
    "product_url",
]


def flatten_products(
    products: Iterable[dict[str, Any]],
    store: StoreInfo | None = None,
) -> list[dict[str, Any]]:
    """
    Flatten a list of Shopify products into one row per variant, suitable for
    a spreadsheet. Products with no variants still emit a single row.
    """
    store_domain = store.domain if store else ""
    myshopify_domain = store.myshopify_domain if store else ""
    rows: list[dict[str, Any]] = []

    for p in products:
        handle = p.get("handle", "")
        tags = p.get("tags")
        if isinstance(tags, list):
            tags = ", ".join(str(t) for t in tags)
        images = p.get("images") or []
        default_image = images[0].get("src") if images else None

        base = {
            "store_domain": store_domain,
            "myshopify_domain": myshopify_domain,
            "product_id": p.get("id"),
            "product_handle": handle,
            "product_title": p.get("title"),
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type"),
            "tags": tags,
            "published_at": p.get("published_at"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
        }

        variants = p.get("variants") or [{}]
        for v in variants:
            # Prefer a variant-specific image, else the product's first image.
            featured = v.get("featured_image") or {}
            image_src = featured.get("src") or default_image

            variant_id = v.get("id")
            product_url = ""
            if store_domain and handle:
                product_url = f"https://{store_domain}/products/{handle}"
                if variant_id:
                    product_url += f"?variant={variant_id}"

            row = dict(base)
            row.update({
                "variant_id": variant_id,
                "variant_title": v.get("title"),
                "sku": v.get("sku"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "available": v.get("available"),
                "requires_shipping": v.get("requires_shipping"),
                "taxable": v.get("taxable"),
                "grams": v.get("grams"),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
                "position": v.get("position"),
                "image_src": image_src,
                "product_url": product_url,
            })
            rows.append(row)

    return rows


def _ensure_parent_dir(path: str) -> None:
    """Create the parent directory of `path` if it doesn't already exist."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    """
    Parse a response body that is expected to be a JSON object.
    Returns the dict, or None if the body is not valid JSON or is valid JSON
    but not an object (e.g. a bare array/null/scalar), so callers never hit an
    AttributeError from calling .get() on a non-dict.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_csv(rows: list[dict[str, Any]], path: str) -> None:
    _ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# The scraper
# ---------------------------------------------------------------------------
class ShopifyScraper:
    """
    A configurable Shopify scraper.

    Parameters
    ----------
    delay:
        Seconds to sleep between paginated requests (politeness). Default 0.5.
    timeout:
        Per-request timeout in seconds. Default 20.
    max_pages:
        Safety cap on /products.json pagination (250 products per page).
        Default 100 (= up to 25,000 products).
    impersonate:
        curl_cffi browser impersonation target. Default "chrome124".
    proxies:
        Optional list of proxy URLs ("http://user:pass@host:port"). When set,
        requests are routed through a random proxy and rotate on failure.
        Use `ShopifyScraper.load_proxies(path)` to read a proxies file.
    proxy_retries:
        How many different proxies to try before giving up. Default 3.
    network_retries:
        How many times to retry the whole client chain when every HTTP client
        fails outright (transient connection/DNS/TLS blips). Default 2.
    """

    def __init__(
        self,
        *,
        delay: float = 0.5,
        timeout: int = 20,
        max_pages: int = 100,
        impersonate: str = "chrome124",
        proxies: list[str] | None = None,
        proxy_retries: int = 3,
        network_retries: int = 2,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.max_pages = max_pages
        self.impersonate = impersonate
        self.proxies = list(proxies) if proxies else []
        self.proxy_retries = proxy_retries
        self.network_retries = max(1, network_retries)

    # ------------------------------------------------------------------ #
    # Proxy loading
    # ------------------------------------------------------------------ #
    @staticmethod
    def load_proxies(path: str) -> list[str]:
        """
        Read a proxies file. One proxy per line, '#' starts a comment.
        Accepts either:
            host:port:user:pass
            host:port
        Returns a list of "http://[user:pass@]host:port" URLs.
        """
        proxies: list[str] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pw = parts
                    proxies.append(f"http://{user}:{pw}@{host}:{port}")
                elif len(parts) == 2:
                    host, port = parts
                    proxies.append(f"http://{host}:{port}")
                else:
                    logger.warning("Skipping malformed proxy line: %s", line)
        logger.info("Loaded %d proxies from %s", len(proxies), path)
        return proxies

    # ------------------------------------------------------------------ #
    # Low-level HTTP - tries every available client, then the proxy pool
    # ------------------------------------------------------------------ #
    def _request(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: int | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, str, str]:
        """
        Perform a GET. Returns (status_code, body_text, final_url).

        With proxies configured, rotates through up to `proxy_retries` random
        proxies, retrying on block/temporary statuses, and falls back to a
        direct request only if every proxy fails. Without proxies, goes direct.
        """
        timeout = timeout if timeout is not None else self.timeout

        if not self.proxies:
            return self._direct_get(url, headers, timeout, follow_redirects)

        candidates = random.sample(
            self.proxies, min(self.proxy_retries, len(self.proxies))
        )
        last: tuple[int, str, str] = (0, "", url)
        for attempt, proxy in enumerate(candidates, 1):
            label = proxy.split("@")[-1]
            logger.info("Proxy attempt %d/%d for %s via %s", attempt, len(candidates), url, label)
            try:
                # retries=1: proxy rotation is itself the retry, so don't also
                # multiply the per-client backoff for a dead proxy.
                status, body, final = self._direct_get(
                    url, headers, timeout, follow_redirects, proxy=proxy, retries=1
                )
            except ShopifyScraperError as exc:
                logger.warning("Proxy %s failed: %s", label, exc)
                continue
            if status not in _PROXY_TRIGGER_STATUSES:
                return status, body, final
            logger.warning("Proxy %s returned %d - rotating", label, status)
            last = (status, body, final)

        logger.warning("All proxies exhausted for %s - trying a direct request", url)
        try:
            return self._direct_get(url, headers, timeout, follow_redirects)
        except ShopifyScraperError:
            return last

    def _direct_get(
        self,
        url: str,
        headers: dict[str, str],
        timeout: int,
        follow_redirects: bool,
        *,
        proxy: str | None = None,
        retries: int | None = None,
    ) -> tuple[int, str, str]:
        """
        Try each HTTP client in order until one returns a response. If every
        client fails outright (a transient network blip), retry the whole chain
        up to `retries` times (default `network_retries`) with a short backoff.
        """
        attempts = self.network_retries if retries is None else max(1, retries)
        errors: list[str] = []
        for attempt in range(1, attempts + 1):
            errors = []
            for name, fn in (
                ("curl_cffi", self._get_curl_cffi),
                ("tls_client", self._get_tls_client),
                ("httpx", self._get_httpx),
                ("urllib", self._get_urllib),
            ):
                try:
                    return fn(url, headers, timeout, follow_redirects, proxy)
                except ImportError:
                    continue  # client not installed - try the next one
                except Exception as exc:  # noqa: BLE001 - deliberately broad; try next client
                    errors.append(f"{name}: {exc}")
                    logger.debug("%s failed for %s: %s", name, url, exc)
            if attempt < attempts:
                backoff = 1.5 * attempt
                logger.warning(
                    "All HTTP clients failed for %s (attempt %d/%d) - retrying in %.1fs",
                    url, attempt, attempts, backoff,
                )
                time.sleep(backoff)
        raise ShopifyScraperError(
            f"All HTTP clients failed for {url} :: " + " | ".join(errors)
        )

    # -- individual clients ------------------------------------------------
    def _get_curl_cffi(self, url, headers, timeout, follow_redirects, proxy):
        from curl_cffi import requests as cffi  # type: ignore

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": follow_redirects,
            "impersonate": self.impersonate,
        }
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        resp = cffi.get(url, **kwargs)
        return resp.status_code, resp.text, str(resp.url)

    def _get_tls_client(self, url, headers, timeout, follow_redirects, proxy):
        import tls_client  # type: ignore

        session = tls_client.Session(
            client_identifier="chrome_120",
            random_tls_extension_order=True,
        )
        # tls_client does not decode brotli/zstd - request only what it handles.
        hdrs = {**headers, "accept-encoding": "gzip, deflate"}
        kwargs: dict[str, Any] = {
            "headers": hdrs,
            "timeout_seconds": timeout,
            "allow_redirects": follow_redirects,
        }
        if proxy:
            kwargs["proxy"] = proxy
        resp = session.get(url, **kwargs)
        return resp.status_code, resp.text, getattr(resp, "url", url) or url

    def _get_httpx(self, url, headers, timeout, follow_redirects, proxy):
        import httpx  # type: ignore

        # Let httpx negotiate its own accept-encoding so it only advertises
        # codecs it can actually decode.
        hdrs = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
        client_kwargs: dict[str, Any] = {
            "follow_redirects": follow_redirects,
            "timeout": timeout,
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url, headers=hdrs)
            return resp.status_code, resp.text, str(resp.url)

    def _get_urllib(self, url, headers, timeout, follow_redirects, proxy):
        import gzip
        import ssl
        import urllib.error
        import urllib.request
        import zlib

        # urllib does not auto-decompress; advertise only gzip/deflate and
        # decode manually below.
        hdrs = {**headers, "accept-encoding": "gzip, deflate"}

        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            # Ignore any ambient system/env proxies for a clean direct request.
            handlers.append(urllib.request.ProxyHandler({}))
        if not follow_redirects:
            handlers.append(_NoRedirect())
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        opener = urllib.request.build_opener(*handlers)

        req = urllib.request.Request(url, headers=hdrs, method="GET")

        def _decode(raw: bytes, enc: str) -> str:
            enc = (enc or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8", errors="replace")

        try:
            with opener.open(req, timeout=timeout) as resp:
                body = _decode(resp.read(), resp.headers.get("Content-Encoding", ""))
                return getattr(resp, "status", 200), body, resp.geturl()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx still carry a body we want to inspect (e.g. 404 JSON).
            body = _decode(exc.read(), exc.headers.get("Content-Encoding", "") if exc.headers else "")
            return exc.code, body, exc.geturl()

    # ------------------------------------------------------------------ #
    # Store discovery
    # ------------------------------------------------------------------ #
    def resolve_store(self, store_url: str) -> StoreInfo:
        """
        Resolve any Shopify store URL to its myshopify.com domain + currency.

        Strategy:
          1. Follow redirects on the URL and read the HTML.
          2. Look for *.myshopify.com in the final URL, then in the HTML body.
          3. Fall back to probing /products.json on the host itself.

        Raises NotAShopifyStoreError if it cannot be resolved.
        """
        url = _normalize_url(store_url)
        logger.info("Resolving Shopify domain for: %s", url)

        try:
            status, body, final_url = self._request(url, BROWSER_HEADERS, follow_redirects=True)
        except ShopifyScraperError as exc:
            raise NotAShopifyStoreError(f"Could not reach store URL: {url}") from exc

        if status in _BLOCKED_STATUSES:
            raise StoreBlockedError(
                f"Store returned {status} (password-protected or blocking scrapers): {url}"
            )

        currency = _extract_currency(body)
        name = _extract_store_name(body)
        public_host = urlparse(final_url).netloc.lower()
        if public_host.startswith("www."):
            public_host = public_host[4:]

        # 1) myshopify domain in the final URL
        m = MYSHOPIFY_RE.search(final_url)
        if m:
            domain = m.group(1).lower()
            logger.info("Found myshopify domain in final URL: %s (%s)", domain, currency)
            return StoreInfo(store_url, public_host or domain, domain, currency, name)

        # 2) myshopify domain in the HTML body - prefer the store's own
        #    canonical reference (Shopify.shop / myshopifyDomain) over the first
        #    generic *.myshopify.com match, which could belong to an embedded
        #    app or a linked store.
        m = MYSHOPIFY_CANONICAL_RE.search(body) or MYSHOPIFY_RE.search(body)
        if m:
            domain = m.group(1).lower()
            logger.info("Found myshopify domain in HTML: %s (%s)", domain, currency)
            return StoreInfo(store_url, public_host or domain, domain, currency, name)

        # 3) the host itself might serve Shopify natively
        if public_host and self.is_shopify_store(public_host):
            logger.info("Host serves Shopify products.json directly: %s (%s)", public_host, currency)
            return StoreInfo(store_url, public_host, public_host, currency, name)

        raise NotAShopifyStoreError(
            f"No myshopify.com reference found for {store_url}. "
            "This does not appear to be a Shopify store."
        )

    def is_shopify_store(self, domain: str) -> bool:
        """Quick probe: does <domain>/products.json return valid Shopify JSON?"""
        url = f"https://{domain}/products.json?limit=1"
        try:
            status, body, _ = self._request(url, JSON_HEADERS, timeout=min(self.timeout, 15))
        except ShopifyScraperError:
            return False
        if status != 200:
            return False
        data = _parse_json_object(body)
        return data is not None and isinstance(data.get("products"), list)

    # ------------------------------------------------------------------ #
    # Product fetching
    # ------------------------------------------------------------------ #
    def fetch_all_products(self, domain: str) -> list[dict[str, Any]]:
        """
        Download every product by paginating /products.json until an empty
        page is returned (or `max_pages` is hit).
        """
        products: list[dict[str, Any]] = []
        base = f"https://{domain}/products.json"
        page = 1
        rate_limit_hits = 0

        while page <= self.max_pages:
            url = f"{base}?limit=250&page={page}"
            logger.info("Fetching products page %d from %s", page, domain)
            try:
                status, body, _ = self._request(url, JSON_HEADERS, timeout=max(self.timeout, 30))
            except ShopifyScraperError as exc:
                logger.error("Failed to fetch page %d: %s", page, exc)
                break

            if status == 429:
                rate_limit_hits += 1
                if rate_limit_hits > _MAX_RATE_LIMIT_RETRIES:
                    logger.warning(
                        "Still rate limited on page %d after %d retries - stopping",
                        page, _MAX_RATE_LIMIT_RETRIES,
                    )
                    break
                wait = 5 * rate_limit_hits
                logger.warning(
                    "Rate limited on page %d (retry %d/%d) - sleeping %ds",
                    page, rate_limit_hits, _MAX_RATE_LIMIT_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            rate_limit_hits = 0  # reset after any non-429 response

            if status in _BLOCKED_STATUSES:
                raise StoreBlockedError(
                    f"Store {domain} returned {status} - password-protected or blocking scrapers."
                )
            if status != 200:
                logger.warning("Non-200 status %d on page %d - stopping", status, page)
                break

            data = _parse_json_object(body)
            if data is None:
                logger.error("Invalid/non-object JSON on page %d - stopping", page)
                break
            batch = data.get("products", [])

            if not batch:
                logger.info("Empty page %d - catalogue complete", page)
                break

            products.extend(batch)
            logger.info("  +%d products (total %d)", len(batch), len(products))
            page += 1
            if self.delay:
                time.sleep(self.delay)
        else:
            logger.warning("Reached max_pages=%d - output may be truncated", self.max_pages)

        return products

    def fetch_product(self, domain: str, handle: str) -> dict[str, Any] | None:
        """Fetch a single product by handle via /products/{handle}.json. None on 404."""
        url = f"https://{domain}/products/{handle}.json"
        status, body, _ = self._request(url, JSON_HEADERS)
        if status == 404:
            return None
        if status in _BLOCKED_STATUSES:
            raise StoreBlockedError(f"Store {domain} returned {status} for product {handle}.")
        if status != 200:
            raise ShopifyScraperError(f"Failed to fetch product {handle}: status {status}")
        data = _parse_json_object(body)
        if data is None:
            raise ShopifyScraperError(f"Invalid JSON fetching product {handle}")
        return data.get("product")

    # ------------------------------------------------------------------ #
    # Collections
    # ------------------------------------------------------------------ #
    def fetch_collections(self, domain: str) -> list[dict[str, Any]]:
        """Download all collections by paginating /collections.json."""
        collections: list[dict[str, Any]] = []
        base = f"https://{domain}/collections.json"
        page = 1
        rate_limit_hits = 0

        while page <= self.max_pages:
            url = f"{base}?limit=250&page={page}"
            logger.info("Fetching collections page %d from %s", page, domain)
            try:
                status, body, _ = self._request(url, JSON_HEADERS)
            except ShopifyScraperError as exc:
                logger.error("Failed to fetch collections page %d: %s", page, exc)
                break

            if status == 429:
                rate_limit_hits += 1
                if rate_limit_hits > _MAX_RATE_LIMIT_RETRIES:
                    logger.warning("Still rate limited on collections page %d - stopping", page)
                    break
                wait = 5 * rate_limit_hits
                logger.warning("Rate limited on collections page %d - sleeping %ds", page, wait)
                time.sleep(wait)
                continue
            rate_limit_hits = 0

            if status in _BLOCKED_STATUSES:
                # Secondary data - warn and return what we have rather than abort.
                logger.warning("Collections blocked (%d) on %s - skipping collections", status, domain)
                break
            if status != 200:
                logger.warning("Non-200 status %d on collections page %d - stopping", status, page)
                break

            data = _parse_json_object(body)
            if data is None:
                logger.error("Invalid/non-object JSON on collections page %d - stopping", page)
                break
            batch = data.get("collections", [])

            if not batch:
                break
            collections.extend(batch)
            logger.info("  +%d collections (total %d)", len(batch), len(collections))
            page += 1
            if self.delay:
                time.sleep(self.delay)

        return collections

    def fetch_collection_products(self, domain: str, handle: str) -> list[dict[str, Any]]:
        """Download all products in a single collection."""
        products: list[dict[str, Any]] = []
        base = f"https://{domain}/collections/{handle}/products.json"
        page = 1
        rate_limit_hits = 0

        while page <= self.max_pages:
            url = f"{base}?limit=250&page={page}"
            try:
                status, body, _ = self._request(url, JSON_HEADERS, timeout=max(self.timeout, 30))
            except ShopifyScraperError as exc:
                logger.error("Failed to fetch collection %s page %d: %s", handle, page, exc)
                break

            if status == 429:
                rate_limit_hits += 1
                if rate_limit_hits > _MAX_RATE_LIMIT_RETRIES:
                    logger.warning("Still rate limited on collection %s page %d - stopping", handle, page)
                    break
                time.sleep(5 * rate_limit_hits)
                continue
            rate_limit_hits = 0

            if status != 200:
                break

            data = _parse_json_object(body)
            if data is None:
                break
            batch = data.get("products", [])

            if not batch:
                break
            products.extend(batch)
            page += 1
            if self.delay:
                time.sleep(self.delay)

        return products

    # ------------------------------------------------------------------ #
    # Live variant price/stock (lightweight per-variant endpoint)
    # ------------------------------------------------------------------ #
    def fetch_variant(
        self,
        domain: str,
        variant_id: int | str,
    ) -> dict[str, Any] | None:
        """
        Fetch live availability + price for one variant via /variants/{id}.js.
        NOTE: price is returned in CENTS (e.g. 10980 == $109.80).
        Returns None on 404 (variant removed).
        """
        url = f"https://{domain}/variants/{variant_id}.js"
        headers = {**JSON_HEADERS, "sec-fetch-site": "same-origin"}

        for attempt in range(2):
            status, body, _ = self._request(url, headers, timeout=min(self.timeout, 15))
            if status == 429:
                if attempt == 0:
                    logger.warning("Rate limited on variant %s - retrying in 5s", variant_id)
                    time.sleep(5)
                    continue
                raise ShopifyScraperError(f"Rate limited fetching variant {variant_id}")
            if status in _BLOCKED_STATUSES:
                raise StoreBlockedError(f"Store {domain} returned {status} on variant {variant_id}.")
            if status == 404:
                return None
            if status != 200:
                raise ShopifyScraperError(f"Failed to fetch variant {variant_id}: status {status}")
            data = _parse_json_object(body)
            if data is None:
                raise ShopifyScraperError(f"Invalid JSON fetching variant {variant_id}")
            return data
        return None

    # ------------------------------------------------------------------ #
    # High-level orchestrator
    # ------------------------------------------------------------------ #
    def scrape(
        self,
        store_url: str,
        *,
        collections: bool = False,
    ) -> ScrapeResult:
        """
        Resolve the store and download its full catalogue in one call.

        Set collections=True to also download every collection.
        """
        store = self.resolve_store(store_url)
        products = self.fetch_all_products(store.myshopify_domain)

        cols: list[dict[str, Any]] = []
        if collections:
            cols = self.fetch_collections(store.myshopify_domain)

        return ScrapeResult(
            store=store,
            products=products,
            collections=cols,
            scraped_at=_utc_now_iso(),
        )


# ---------------------------------------------------------------------------
# Small stdlib helpers
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty URL")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_currency(body: str) -> str:
    m = CURRENCY_RE.search(body or "")
    return m.group(1).upper() if m else "USD"


def _extract_store_name(body: str) -> str | None:
    m = OG_SITE_NAME_RE.search(body or "")
    if m:
        return m.group(1).strip()
    m = TITLE_RE.search(body or "")
    if m:
        return m.group(1).strip()
    return None


def extract_product_url_parts(product_url: str) -> tuple[str, str, str | None]:
    """
    Parse a Shopify product URL, e.g.
        https://gymshark.com/products/speed-shorts?variant=123
    Returns (store_domain, handle, variant_id_or_none).
    """
    url = _normalize_url(product_url)
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    try:
        handle = parts[parts.index("products") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(
            f"Could not parse a product handle from: {product_url}. "
            "Expected .../products/<handle>"
        ) from exc
    variant_id = parse_qs(parsed.query).get("variant", [None])[0]
    return parsed.netloc.lower(), handle, variant_id


class _NoRedirect:
    """urllib handler that turns off redirect following."""

    # Built as a subclass at runtime to avoid importing urllib at module load.
    def __new__(cls):  # noqa: D401
        import urllib.request

        class _Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: D401, ANN001
                return None

        return _Handler()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _looks_like_product_url(url: str) -> bool:
    return "/products/" in urlparse(_normalize_url(url)).path


def _default_basename(store: StoreInfo) -> str:
    host = store.myshopify_domain or store.domain
    return re.sub(r"[^\w.-]", "_", host.replace(".myshopify.com", "") or "shopify_store")


def _resolve_output_targets(output: str | None, fmt: str, store: StoreInfo) -> dict[str, str]:
    """
    Decide the JSON/CSV output paths from the user's --output value and format.
    Returns {"json": path} and/or {"csv": path}.
    """
    formats = ["json", "csv"] if fmt == "both" else [fmt]
    base = _default_basename(store)

    # Treat as a directory if it exists as one or ends with a path separator.
    is_dir = output is not None and (
        os.path.isdir(output) or output.endswith(("/", "\\"))
    )

    targets: dict[str, str] = {}
    if output and not is_dir:
        # Explicit file path.
        if fmt != "both":
            targets[fmt] = output          # single format - honour it verbatim
            return targets
        # For "both", keep the user's folder + filename stem, vary the extension
        # (so `-o out.json -f both` yields out.json AND out.csv).
        directory = os.path.dirname(output)
        stem = os.path.splitext(os.path.basename(output))[0] or base
        for f in formats:
            targets[f] = os.path.join(directory, f"{stem}.{f}") if directory else f"{stem}.{f}"
        return targets

    # A directory was given (or nothing → current working directory).
    directory = output if is_dir else ""
    for f in formats:
        targets[f] = os.path.join(directory, f"{base}.{f}") if directory else f"{base}.{f}"
    return targets


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shopify_scraper",
        description="All-in-one open-source Shopify store scraper. "
                    "Works with Shopify stores only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  shopify_scraper.py gymshark.com\n"
            "  shopify_scraper.py https://store.jeep.com -f both -o out/\n"
            "  shopify_scraper.py gymshark.com --collections\n"
            "  shopify_scraper.py https://gymshark.com/products/speed-shorts\n"
            "  shopify_scraper.py gymshark.com --check\n"
        ),
    )
    p.add_argument("store_url", help="Shopify store URL, or a product URL (.../products/<handle>)")
    p.add_argument("-o", "--output", help="Output file OR directory (default: <store>.json in cwd)")
    p.add_argument("-f", "--format", choices=["json", "csv", "both"], default="json",
                   help="Output format (default: json)")
    p.add_argument("--collections", action="store_true", help="Also scrape all collections")
    p.add_argument("--check", action="store_true",
                   help="Only report whether the URL is a Shopify store, then exit")
    p.add_argument("--variant", metavar="ID",
                   help="Fetch live price/stock for one variant id and exit")
    p.add_argument("--max-pages", type=int, default=100,
                   help="Max pages of 250 products to fetch (default: 100 = 25,000 products)")
    p.add_argument("--delay", type=float, default=0.5,
                   help="Delay in seconds between requests (default: 0.5)")
    p.add_argument("--timeout", type=int, default=20, help="Per-request timeout (default: 20)")
    p.add_argument("--impersonate", default="chrome124",
                   help="curl_cffi impersonation target (default: chrome124)")
    p.add_argument("--proxies", metavar="FILE",
                   help="Proxies file (host:port:user:pass per line) for rotation")
    p.add_argument("-q", "--quiet", action="store_true", help="Only show warnings and errors")
    p.add_argument("-v", "--verbose", action="store_true", help="Show debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    # Make emoji/arrow output safe when stdout/stderr is redirected on Windows
    # (the default console code page, e.g. cp1252, can't encode them).
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    args = _build_arg_parser().parse_args(argv)

    level = logging.INFO
    if args.quiet:
        level = logging.WARNING
    elif args.verbose:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    proxies = None
    if args.proxies:
        try:
            proxies = ShopifyScraper.load_proxies(args.proxies)
        except OSError as exc:
            print(f"error: could not read proxies file: {exc}", file=sys.stderr)
            return 2

    scraper = ShopifyScraper(
        delay=args.delay,
        timeout=args.timeout,
        max_pages=args.max_pages,
        impersonate=args.impersonate,
        proxies=proxies,
    )

    try:
        # --- quick check mode ---
        if args.check:
            store = scraper.resolve_store(args.store_url)
            print(f"✅ Shopify store confirmed")
            print(f"   name:      {store.name or '(unknown)'}")
            print(f"   domain:    {store.domain}")
            print(f"   myshopify: {store.myshopify_domain}")
            print(f"   currency:  {store.currency}")
            return 0

        # --- single live variant mode ---
        if args.variant:
            store = scraper.resolve_store(args.store_url)
            data = scraper.fetch_variant(store.myshopify_domain, args.variant)
            if data is None:
                print(f"Variant {args.variant} not found (404).")
                return 1
            price = data.get("price")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            if isinstance(price, (int, float)):
                print(f"\nprice: {price / 100:.2f} {store.currency}   available: {data.get('available')}")
            return 0

        # --- single product mode (product URL given) ---
        if _looks_like_product_url(args.store_url):
            store = scraper.resolve_store(args.store_url)
            _, handle, _ = extract_product_url_parts(args.store_url)
            logger.info("Fetching single product '%s'", handle)
            product = scraper.fetch_product(store.myshopify_domain, handle)
            if product is None:
                print(f"Product '{handle}' not found (404).")
                return 1
            result = ScrapeResult(store=store, products=[product], scraped_at=_utc_now_iso())
        else:
            # --- full store scrape ---
            result = scraper.scrape(args.store_url, collections=args.collections)

        # --- report + export ---
        store = result.store
        print(f"\n{'=' * 60}")
        print(f"Store:      {store.name or store.domain}")
        print(f"Domain:     {store.domain}  →  {store.myshopify_domain}")
        print(f"Currency:   {store.currency}")
        print(f"Products:   {result.product_count}")
        print(f"Variants:   {result.variant_count}")
        if result.collections:
            print(f"Collections:{len(result.collections):>4}")
        print(f"{'=' * 60}\n")

        if result.product_count == 0:
            print("No products returned - the store may be empty, password-protected, "
                  "or blocking scrapers.")
            return 1

        targets = _resolve_output_targets(args.output, args.format, store)
        if "json" in targets:
            result.save_json(targets["json"])
            print(f"  JSON → {targets['json']}")
        if "csv" in targets:
            result.save_csv(targets["csv"])
            print(f"  CSV  → {targets['csv']}")
        return 0

    except NotAShopifyStoreError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except StoreBlockedError as exc:
        print(f"🚫 {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except ValueError as exc:
        # Bad user input (empty URL, unparseable product URL, etc.)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ShopifyScraperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
