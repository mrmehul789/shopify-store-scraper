# Shopify Scraper: Free Open Source Shopify Product Scraper in Python

Shopify Scraper is a free, open source, all in one **Shopify store scraper** written in Python. Point it at any Shopify store URL (custom domain or `*.myshopify.com`) and it downloads the full product catalog to JSON and CSV with a single command. No API key, no login, and no paid service required.

Use it to scrape Shopify products, prices, variants, SKUs, inventory status, images, and collections for price monitoring, competitor analysis, dropshipping product research, or catalog export.

> This tool works with **Shopify stores only**. It reads Shopify's public JSON endpoints (`/products.json`, `/collections.json`, `/variants/{id}.js`) and does not work on non Shopify websites.

## Table of contents

- [Features](#features)
- [Use cases](#use-cases)
- [Installation](#installation)
- [Usage](#usage)
- [Command line options](#command-line-options)
- [Python library usage](#python-library-usage)
- [Output format](#output-format)
- [Limits](#limits)
- [Proxies](#proxies)
- [FAQ](#faq)
- [Legal and etiquette](#legal-and-etiquette)
- [License](#license)

## Features

- **Auto detect the store.** Resolves any URL (custom domain or `myshopify.com`) to its underlying `*.myshopify.com` domain and reads the store currency.
- **Full catalog scrape.** Paginates `/products.json` to grab every product and variant (price, SKU, stock, images, options), up to **25,000 products** per store.
- **Collections.** Optionally pull every collection from the store.
- **Single product and live variant.** Fetch one product by handle, or live price and stock for a single variant via the lightweight `/variants/{id}.js` endpoint.
- **Export to JSON and CSV.** Save raw JSON, or a flat CSV with one row per variant, ready for Excel or Google Sheets.
- **Resilient HTTP.** Tries `curl_cffi`, then `tls_client`, then `httpx`, then `urllib`, with TLS fingerprint spoofing for bot protected stores, transient failure retries, optional proxy rotation, and polite rate limiting.
- **Zero required dependencies.** Runs on the Python standard library alone.

## Use cases

- Export a Shopify store's full product catalog to CSV or JSON.
- Monitor Shopify product prices and stock over time.
- Competitor and market research for ecommerce and dropshipping.
- Bulk collect product data, variants, SKUs, and images from Shopify stores.
- Validate whether a website is built on Shopify.

## Installation

Requires **Python 3.9 or newer**. Clone or copy `shopify_scraper.py`, which is the whole tool. For the best success rate against protected stores, install the optional HTTP clients:

```bash
pip install -r requirements.txt
```

The scraper still runs with none of them installed, using the Python standard library as a fallback.

## Usage

### Scrape all products from a Shopify store

```bash
python shopify_scraper.py gymshark.com
```

This writes `gymshark.myshopify.json` in the current folder.

### Check if a URL is a Shopify store

Use `--check` to quickly verify that a URL is a Shopify store without downloading any products. It resolves the store and prints the store name, public domain, underlying `myshopify.com` domain, and currency, then exits. This is useful for validating a URL, or a list of URLs, before running a full scrape.

```bash
python shopify_scraper.py gymshark.com --check
```

Example output:

```
Shopify store confirmed
   name:      Gymshark
   domain:    gymshark.com
   myshopify: gymshark.myshopify.com
   currency:  USD
```

### Export Shopify products to CSV

```bash
python shopify_scraper.py https://store.jeep.com -f both -o out/
```

`-f both` writes both a JSON file and a CSV file. `-o out/` puts them in an `out` folder.

### Include collections

```bash
python shopify_scraper.py gymshark.com --collections
```

### Scrape a single product

Pass a product URL and the scraper fetches just that product:

```bash
python shopify_scraper.py https://gymshark.com/products/speed-shorts
```

### Get live price and stock for one variant

```bash
python shopify_scraper.py gymshark.com --variant 1234567890
```

### Route through rotating proxies

```bash
python shopify_scraper.py gymshark.com --proxies proxies.txt
```

## Command line options

| Flag | Description |
|------|-------------|
| `-o, --output` | Output file or directory (default: `<store>.json` in the current folder) |
| `-f, --format` | `json`, `csv`, or `both` (default: `json`) |
| `--collections` | Also scrape all collections |
| `--check` | Only report whether the URL is a Shopify store, then exit |
| `--variant ID` | Fetch live price and stock for one variant, then exit |
| `--max-pages N` | Max pages of 250 products (default: 100, which is 25,000 products) |
| `--delay S` | Delay between requests in seconds (default: 0.5) |
| `--timeout S` | Per request timeout in seconds (default: 20) |
| `--impersonate` | `curl_cffi` browser target (default: `chrome124`) |
| `--proxies FILE` | Proxies file (`host:port:user:pass` per line) for rotation |
| `-q, --quiet` | Show warnings and errors only |
| `-v, --verbose` | Show debug logging |

## Python library usage

```python
from shopify_scraper import ShopifyScraper

scraper = ShopifyScraper(delay=0.5, max_pages=100)

# One shot full scrape
result = scraper.scrape("https://gymshark.com", collections=True)
print(result.store.myshopify_domain, result.store.currency)
print(result.product_count, "products /", result.variant_count, "variants")
result.save_json("gymshark.json")
result.save_csv("gymshark.csv")

# Or call the pieces directly
store = scraper.resolve_store("gymshark.com")
products = scraper.fetch_all_products(store.myshopify_domain)
one = scraper.fetch_product(store.myshopify_domain, "speed-shorts")
live = scraper.fetch_variant(store.myshopify_domain, 1234567890)  # price in cents
```

## Output format

**JSON** contains a `store` block (domain, myshopify domain, currency, name), counts, the raw `products` array exactly as Shopify returns it, and any `collections`.

**CSV** is flattened to one row per variant with these columns:

```
store_domain, myshopify_domain, product_id, product_handle, product_title,
vendor, product_type, tags, published_at, created_at, updated_at, variant_id,
variant_title, sku, price, compare_at_price, available, requires_shipping,
taxable, grams, option1, option2, option3, position, image_src, product_url
```

## Limits

This scraper fetches a **maximum of 25,000 products per store**. If a store has more than 25,000 products, only the first 25,000 are scraped and the rest are skipped.

## Proxies

Create a `proxies.txt` file with one proxy per line (`#` starts a comment):

```
host:port:user:pass
host:port
```

Pass it with `--proxies proxies.txt`. The scraper picks a random proxy and rotates to another on blocks or timeouts, falling back to a direct request only if every proxy fails.

## FAQ

**How do I scrape all products from a Shopify store?**
Run `python shopify_scraper.py <store-url>`. The scraper resolves the store and downloads every product to a JSON file.

**Does it work on any Shopify store?**
It works on Shopify stores, including stores that use a custom domain instead of a `myshopify.com` address. It does not work on non Shopify websites.

**Do I need a Shopify API key or app?**
No. The scraper only reads public JSON endpoints that a normal browser can reach. No API key, app, or login is needed.

**How do I export Shopify products to a CSV file?**
Use `-f csv` for CSV only, or `-f both` for JSON and CSV. Each row in the CSV is one product variant.

**Can it scrape prices, stock, and variants?**
Yes. Each variant includes price, compare at price, availability, SKU, and options. Use `--variant` for a live price and stock check on a single variant.

**How many products can it scrape?**
Up to 25,000 products per store. See [Limits](#limits).

**Is it free and open source?**
Yes. It is released under the MIT license and has no paid tiers.

**What Python version do I need?**
Python 3.9 or newer.

## Legal and etiquette

This tool only reads **public** endpoints that a normal browser can reach. You are responsible for using it lawfully. Respect each store's Terms of Service and `robots.txt`, keep request rates reasonable with `--delay`, and do not use scraped data in ways that infringe the store's rights.

## License

MIT. See [LICENSE](LICENSE).
