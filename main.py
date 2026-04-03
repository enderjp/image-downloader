from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import sys
from rich import print
from rich.console import Console
from rich.table import Table

load_dotenv()

DEFAULT_TIMEOUT = float(os.environ.get("FACEBOOK_REQUEST_TIMEOUT", "8"))
DEFAULT_DELAY = float(os.environ.get("FACEBOOK_REQUEST_DELAY", "0.6"))
DEFAULT_MAX_DEPTH = int(os.environ.get("FACEBOOK_MAX_VARIANTS", "6"))
FACEBOOK_PROXY_URL = os.environ.get("FACEBOOK_PROXY_URL")

USER_AGENTS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
   # "Mozilla/5.0 (Linux; Android 10; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
  #  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class FetchLog:
    url: str
    status: Optional[int]
    took_ms: float
    agent: str
    error: Optional[str]


@dataclass
class ScrapeResult:
    original_url: str
    variants_tried: List[FetchLog]
    images: List[str]
    script: Optional[str] = None


class ScrapePayload(BaseModel):
    url: str = Field(..., description="URL pública de Facebook")
    proxy: Optional[str] = Field(None, description="Proxy HTTP/S opcional para esta solicitud")
    cookies: Optional[str] = Field(None, description="Encabezado Cookie (name=value; ...) para sesiones autenticadas")
    include_mobile: bool = Field(True, description="Probar variantes m./mbasic")
    max_depth: int = Field(DEFAULT_MAX_DEPTH, ge=1, le=20, description="Máximo de variantes a generar")
    delay: Optional[float] = Field(None, ge=0.0, description="Delay entre requests en segundos")
    timeout: Optional[float] = Field(None, gt=0.0, description="Timeout por request en segundos")


class FetchLogModel(BaseModel):
    url: str
    status: Optional[int]
    took_ms: float
    agent: str
    error: Optional[str]

    @classmethod
    def from_dataclass(cls, log: FetchLog) -> "FetchLogModel":
        return cls(**asdict(log))


class ScrapeResultModel(BaseModel):
    original_url: str
    variants_tried: List[FetchLogModel]
    images: List[str]
    script: Optional[str]

    @classmethod
    def from_dataclass(cls, result: ScrapeResult) -> "ScrapeResultModel":
        return cls(
            original_url=result.original_url,
            variants_tried=[FetchLogModel.from_dataclass(log) for log in result.variants_tried],
            images=result.images,
            script=result.script,
        )


class PlaywrightResultModel(BaseModel):
    original_url: str
    script: Optional[str]


def normalize_url(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("URL inválida")
    cleaned = parsed._replace(fragment="")
    return urlunparse(cleaned)


def candidate_variants(url: str, include_mobile: bool = True, max_depth: int = DEFAULT_MAX_DEPTH) -> List[str]:
    parsed = urlparse(url)
    variants: List[str] = []

    def add_variant(replacement: Tuple[str, str]) -> None:
        scheme, netloc = replacement
        new = parsed._replace(scheme=scheme or parsed.scheme, netloc=netloc)
        variants.append(urlunparse(new))

    variants.append(url)

    netloc = parsed.netloc.lower()
    bare = netloc.replace("www.", "")

    if include_mobile:
        add_variant((parsed.scheme, f"m.{bare}"))
        add_variant((parsed.scheme, f"mbasic.{bare}"))

    if "/share/" not in parsed.path:
        share_url = parsed._replace(path=f"/share/p/{parsed.path.strip('/')}")
        variants.append(urlunparse(share_url))

    dedup: List[str] = []
    for candidate in variants:
        if candidate and candidate not in dedup:
            dedup.append(candidate)
        if len(dedup) >= max_depth:
            break
    return dedup


def build_headers(cookie: Optional[str] = None) -> Dict[str, str]:
    headers = dict(HEADERS_BASE)
    headers["User-Agent"] = random.choice(USER_AGENTS)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def build_proxies(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def is_noise_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if not host:
        return True

    if "emoji.php" in path:
        return True
    if path.startswith("/rsrc.php"):
        return True
    if "hsts-pixel.gif" in path:
        return True
    if host.startswith("static.") and host.endswith("fbcdn.net"):
        return True

    return False


def canonical_image_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    filename = path.rsplit("/", 1)[-1].lower() if path else ""
    query = parse_qs(parsed.query)

    media_id = query.get("media_id", [None])[0]
    if media_id and "lookaside" in host:
        return f"lookaside:{media_id}"

    ad_image_token = query.get("d", [None])[0]
    if host.endswith("facebook.com") and path == "/ads/image" and ad_image_token:
        return f"fbads:{ad_image_token}"

    if host.endswith("fbcdn.net") and filename:
        return f"fbcdn:{filename}"

    if filename:
        return f"{host}:{filename}"

    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)


def image_variant_score(url: str) -> Tuple[int, int, int, int]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    raw = f"{parsed.path}?{parsed.query}"
    dimensions = re.findall(r"(\d{2,5})x(\d{2,5})", raw)
    max_area = 0
    max_edge = 0
    for width_text, height_text in dimensions:
        width = int(width_text)
        height = int(height_text)
        area = width * height
        if area > max_area:
            max_area = area
        if max(width, height) > max_edge:
            max_edge = max(width, height)

    if host.endswith("fbcdn.net") and not host.startswith("static."):
        host_priority = 3
    elif host.endswith("facebook.com") and path == "/ads/image":
        host_priority = 1
    else:
        host_priority = 2
    query_bonus = 1 if not parsed.query else 0

    return (host_priority, max_area, max_edge, query_bonus)


def extract_images(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, str] = {}

    def push(url: Optional[str]) -> None:
        if not url:
            return
        cleaned = url.strip()
        if not cleaned or is_noise_image_url(cleaned):
            return
        key = canonical_image_key(cleaned)
        existing = found.get(key)
        if existing is None or image_variant_score(cleaned) > image_variant_score(existing):
            found[key] = cleaned

    for img in soup.find_all("img"):
        push(img.get("src"))
        push(img.get("data-src"))

    for meta_name in ["og:image", "og:image:url", "og:image:secure_url", "twitter:image"]:
        meta = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
        if meta:
            push(meta.get("content"))

    link_tag = soup.find("link", attrs={"rel": lambda r: r and "image_src" in r})
    if link_tag:
        push(link_tag.get("href"))

    return list(found.values())


def extract_post_text(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # Prefer meta descriptions (og:description, twitter:description, description)
    for name in ("og:description", "twitter:description", "description"):
        meta = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if meta and meta.get("content"):
            text = meta.get("content").strip()
            if text:
                return text

    # Look for explicit post message containers
    el = soup.find(attrs={"data-ad-preview": "message"})
    if el:
        text = el.get_text(" ", strip=True)
        if text:
            return text

    el = soup.find(attrs={"data-testid": lambda v: v and "post_message" in v})
    if el:
        text = el.get_text(" ", strip=True)
        if text:
            return text

    # Try article block fallback
    article = soup.find("article")
    if article:
        text = article.get_text(" ", strip=True)
        if text:
            return text

    # Extra heuristics: look for common Facebook post containers and pick the longest text
    candidates: List[str] = []
    selectors = [
        "div._5pbx",             # old web class
        "div.userContent",       # common class
        "div.story_body_container",
        "div[data-testid='post_message']",
        "div[id^='m_story_permalink_view']",
        "div[id^='u_']",
        "div[role='article']",
        "span[dir='ltr']",
    ]
    for sel in selectors:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                candidates.append(t)

    # Also consider large text blocks (paragraphs) as candidates
    for p in soup.find_all(["p", "div", "span"]):
        t = p.get_text(" ", strip=True)
        if t and len(t) > 60:
            candidates.append(t)

    if candidates:
        # choose the longest candidate, but collapse multiple spaces/newlines
        best = max(candidates, key=lambda s: len(s))
        return "\n\n".join([line.strip() for line in best.splitlines() if line.strip()])

    return None


def fetch_html(url: str, proxy_url: Optional[str], timeout: float, cookie: Optional[str] = None) -> Tuple[Optional[str], FetchLog]:
    headers = build_headers(cookie)
    proxies = build_proxies(proxy_url)
    start = time.perf_counter()
    try:
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True)
        took_ms = (time.perf_counter() - start) * 1000
        if resp.status_code != 200:
            return None, FetchLog(url, resp.status_code, took_ms, headers["User-Agent"], None)
        return resp.text, FetchLog(url, resp.status_code, took_ms, headers["User-Agent"], None)
    except Exception as exc:
        took_ms = (time.perf_counter() - start) * 1000
        return None, FetchLog(url, None, took_ms, headers["User-Agent"], str(exc))


def scrape_images(url: str, *, proxy: Optional[str], cookies: Optional[str] = None, include_mobile: bool, max_depth: int, delay: float, timeout: float) -> ScrapeResult:
    normalized = normalize_url(url)
    variants = candidate_variants(normalized, include_mobile=include_mobile, max_depth=max_depth)
    logs: List[FetchLog] = []
    images_by_key: Dict[str, str] = {}
    script: Optional[str] = None

    for candidate in variants:
        html, log_entry = fetch_html(candidate, proxy, timeout, cookie=cookies)
        logs.append(log_entry)
        if html:
            extracted = extract_images(html)
            for img_url in extracted:
                key = canonical_image_key(img_url)
                existing = images_by_key.get(key)
                if existing is None or image_variant_score(img_url) > image_variant_score(existing):
                    images_by_key[key] = img_url
            if script is None:
                try:
                    text = extract_post_text(html)
                    if text:
                        script = text
                except Exception:
                    # don't fail the whole scrape if text extraction has an issue
                    script = script
        time.sleep(delay)
    return ScrapeResult(normalized, logs, list(images_by_key.values()), script)


def scrape_with_playwright(url: str, *, proxy: Optional[str] = None, cookies: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Render the page with Playwright and extract the post text using the same
    `extract_post_text` helper. This function is isolated and does not touch
    the existing image-extraction flow.
    """
    normalized = normalize_url(url)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - runtime environment may not have playwright
        raise RuntimeError("playwright is not installed. Install it with 'pip install playwright' and run 'playwright install'") from exc

    with sync_playwright() as p:
        # configure proxy for browser launch if provided (Playwright requires global proxy)
        launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
        if proxy:
            parsed = urlparse(proxy)
            proxy_conf = {"server": proxy}
            if parsed.username:
                proxy_conf["username"] = parsed.username
            if parsed.password:
                proxy_conf["password"] = parsed.password
            launch_kwargs["proxy"] = proxy_conf

        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(ignore_https_errors=True)
        header_cookies = cookies if cookies is not None else os.environ.get("FACEBOOK_COOKIES")
        if header_cookies:
            try:
                ctx.set_extra_http_headers({"Cookie": header_cookies})
            except Exception:
                pass

        page = ctx.new_page()
        try:
            page.goto(normalized, wait_until="networkidle", timeout=int(timeout * 1000))
        except Exception:
            # fallback: try a shorter wait
            try:
                page.goto(normalized, timeout=int(timeout * 1000))
            except Exception:
                page.close()
                ctx.close()
                browser.close()
                return None

        html = page.content()
        page.close()
        ctx.close()
        browser.close()

    return extract_post_text(html)


def render_variants_with_playwright(url: str, *, proxy: Optional[str] = None, cookies: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT, include_mobile: bool = True, max_depth: int = DEFAULT_MAX_DEPTH) -> Dict[str, Optional[str]]:
    """Render multiple URL variants with Playwright and return a mapping variant->script.
    This function launches the browser once and iterates over candidate variants to
    reduce startup overhead. It is isolated from the image extraction logic.
    """
    variants = candidate_variants(normalize_url(url), include_mobile=include_mobile, max_depth=max_depth)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError("playwright is not installed. Install it with 'pip install playwright' and run 'playwright install'") from exc

    results: Dict[str, Optional[str]] = {}

    with sync_playwright() as p:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
        if proxy:
            parsed = urlparse(proxy)
            proxy_conf = {"server": proxy}
            if parsed.username:
                proxy_conf["username"] = parsed.username
            if parsed.password:
                proxy_conf["password"] = parsed.password
            launch_kwargs["proxy"] = proxy_conf

        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(ignore_https_errors=True)
        header_cookies = cookies if cookies is not None else os.environ.get("FACEBOOK_COOKIES")
        if header_cookies:
            try:
                ctx.set_extra_http_headers({"Cookie": header_cookies})
            except Exception:
                pass
        page = ctx.new_page()

        def looks_like_login(text: str) -> bool:
            low = text.lower()
            checks = [
                "log in",
                "log into facebook",
                "create new account",
                "email or mobile number",
                "forgot password",
                "sign up",
            ]
            return any(c in low for c in checks)

        for candidate in variants:
            try:
                page.goto(candidate, wait_until="networkidle", timeout=int(timeout * 1000))
            except Exception:
                try:
                    page.goto(candidate, timeout=int(timeout * 1000))
                except Exception:
                    results[candidate] = None
                    continue
            # attempt to expand truncated content by clicking common "see more" buttons
            try:
                # common texts in different languages
                see_more_texts = [
                    "See more",
                    "See More",
                    "Ver más",
                    "Mostrar más",
                    "Leer más",
                    "... ver más",
                    "See more…",
                ]
                clicked = False
                for t in see_more_texts:
                    try:
                        locator = page.locator(f"text=\"{t}\"")
                        if locator.count() > 0:
                            locator.first.click(timeout=2000)
                            page.wait_for_load_state("networkidle", timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue

                # also try buttons with aria-label or role
                if not clicked:
                    try:
                        btn = page.query_selector("button[aria-label*='more']")
                        if btn:
                            btn.click()
                            page.wait_for_load_state("networkidle", timeout=2000)
                            clicked = True
                    except Exception:
                        pass

                # scroll to bottom to trigger lazy load
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                # additional aggressive interactions: try regex-based locator clicks and PageDown presses
                try:
                    # click any 'see more' like text using a regex (case-insensitive)
                    try:
                        locator = page.locator("text=/See more|Mostrar más|Ver más|Leer más/i")
                        cnt = locator.count()
                        for i in range(cnt):
                            try:
                                locator.nth(i).click(timeout=1500)
                                page.wait_for_timeout(400)
                            except Exception:
                                continue
                    except Exception:
                        pass

                    # press PageDown multiple times to expand lazy content
                    for _ in range(6):
                        try:
                            page.keyboard.press("PageDown")
                            page.wait_for_timeout(300)
                        except Exception:
                            break

                    # final scroll-to-top and bottom to ensure dynamic changes
                    try:
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(200)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass

            html = page.content()
            text = extract_post_text(html)
            if text and looks_like_login(text):
                results[candidate] = None
            else:
                results[candidate] = text

        page.close()
        ctx.close()
        browser.close()

    return results


app = FastAPI(title="FB Image Lab API", version="0.1.0", description="Explora imágenes públicas de Facebook")


@app.get("/healthz", summary="Comprobación básica")
def health_check() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResultModel, summary="Obtiene imágenes para una URL pública de Facebook")
def scrape_endpoint(payload: ScrapePayload) -> ScrapeResultModel:
    proxy = payload.proxy if payload.proxy is not None else FACEBOOK_PROXY_URL
    delay = payload.delay if payload.delay is not None else DEFAULT_DELAY
    timeout = payload.timeout if payload.timeout is not None else DEFAULT_TIMEOUT
    try:
        result = scrape_images(
            payload.url,
            proxy=proxy,
            cookies=payload.cookies,
            include_mobile=payload.include_mobile,
            max_depth=payload.max_depth,
            delay=delay,
            timeout=timeout,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ScrapeResultModel.from_dataclass(result)


@app.post("/render", response_model=PlaywrightResultModel, summary="Renderiza la página con Playwright y extrae el texto completo del post")
def render_endpoint(payload: ScrapePayload) -> PlaywrightResultModel:
    proxy = payload.proxy if payload.proxy is not None else FACEBOOK_PROXY_URL
    timeout = payload.timeout if payload.timeout is not None else DEFAULT_TIMEOUT
    try:
        script = scrape_with_playwright(payload.url, proxy=proxy, cookies=payload.cookies, timeout=timeout)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return PlaywrightResultModel(original_url=normalize_url(payload.url), script=script)


def dump_table(result: ScrapeResult) -> None:
    table = Table(title="Intentos")
    table.add_column("URL", overflow="fold")
    table.add_column("Status")
    table.add_column("ms", justify="right")
    table.add_column("UA", overflow="fold")
    table.add_column("Error", overflow="fold")
    for log in result.variants_tried:
        status = str(log.status) if log.status is not None else "-"
        table.add_row(log.url, status, f"{log.took_ms:.0f}", log.agent, log.error or "")
    console = Console()
    console.print(table)
    if result.images:
        console.print(f"[bold green]Encontradas {len(result.images)} imágenes[/bold green]")
        for img in result.images:
            console.print(f"  • {img}")
    else:
        console.print("[bold red]No se encontraron imágenes[/bold red]")
    # Print post text (script) separately to avoid table truncation
    if result.script:
        console.print("\n[bold cyan]Script:[/bold cyan]")
        # Use plain console print so long text is wrapped but not truncated
        console.print(result.script)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe de imágenes para enlaces públicos de Facebook")
    parser.add_argument("--url", required=True, help="URL pública de Facebook (post, share, etc.)")
    parser.add_argument("--proxy", default=FACEBOOK_PROXY_URL, help="Proxy HTTP/S opcional")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout por request (s)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Delay entre intentos (s)")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Máximo de variantes a probar")
    parser.add_argument("--no-mobile", action="store_true", help="No probar variantes m./mbasic")
    parser.add_argument("--json", action="store_true", help="Imprimir resultado en JSON en lugar de tabla")
    parser.add_argument("--out-file", help="Escribir JSON completo a archivo (evita truncado en terminal)")
    parser.add_argument("--print-script", action="store_true", help="Imprimir solo el campo 'script' en crudo (UTF-8)")
    parser.add_argument("--render", action="store_true", help="Usar Playwright para renderizar la página y extraer el texto completo")
    parser.add_argument("--render-variants", action="store_true", help="Renderizar todas las variantes (www/m/mbasic/share) con Playwright y combinar textos")
    parser.add_argument("--cookies", help="Cookie header string 'name=value; ...' para peticiones autenticadas")
    args = parser.parse_args()

    result = scrape_images(
        args.url,
        proxy=args.proxy,
        cookies=args.cookies,
        include_mobile=not args.no_mobile,
        max_depth=args.max_depth,
        delay=args.delay,
        timeout=args.timeout,
    )

    if args.json:
        out = json.dumps(asdict(result), indent=2, ensure_ascii=False)
        if args.out_file:
            try:
                with open(args.out_file, "w", encoding="utf-8") as fh:
                    fh.write(out)
            except Exception as exc:
                sys.stderr.write(f"Error escribiendo {args.out_file}: {exc}\n")
                buf = getattr(sys.stdout, "buffer", None)
                if buf is not None:
                    buf.write(out.encode("utf-8") + b"\n")
                else:
                    sys.stdout.write(out + "\n")
        else:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write(out.encode("utf-8") + b"\n")
            else:
                sys.stdout.write(out + "\n")
        # if render flag is set, do not continue to image-based flow
    if args.render:
        try:
            script = scrape_with_playwright(args.url, proxy=args.proxy, cookies=args.cookies, timeout=args.timeout)
        except RuntimeError as exc:
            print(f"Playwright error: {exc}")
            return
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            if script:
                buf.write(script.encode("utf-8") + b"\n")
        else:
            if script:
                sys.stdout.write(script + "\n")
        return
    if args.render_variants:
        try:
            per_variant = render_variants_with_playwright(args.url, proxy=args.proxy, cookies=args.cookies, timeout=args.timeout, include_mobile=not args.no_mobile, max_depth=args.max_depth)
        except RuntimeError as exc:
            print(f"Playwright error: {exc}")
            return

        # Combine texts: prefer the longest non-empty script; also include per-variant diagnostics
        candidates: List[str] = [t for t in per_variant.values() if t]
        combined: Optional[str]
        if candidates:
            # choose the longest candidate
            combined = max(candidates, key=lambda s: len(s))
        else:
            combined = None

        # output combined script
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            if combined:
                buf.write(combined.encode("utf-8") + b"\n")
        else:
            if combined:
                sys.stdout.write(combined + "\n")

        return
    # If requested, print only the extracted script (raw UTF-8) and exit
    if args.print_script:
        if result.script:
            buf = getattr(sys.stdout, "buffer", None)
            if buf is not None:
                buf.write(result.script.encode("utf-8") + b"\n")
            else:
                sys.stdout.write(result.script + "\n")
        else:
            # no script found -> empty output
            pass
        return
    else:
        dump_table(result)


if __name__ == "__main__":
    main()
