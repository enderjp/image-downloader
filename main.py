from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
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


class ScrapePayload(BaseModel):
    url: str = Field(..., description="URL pública de Facebook")
    proxy: Optional[str] = Field(None, description="Proxy HTTP/S opcional para esta solicitud")
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

    @classmethod
    def from_dataclass(cls, result: ScrapeResult) -> "ScrapeResultModel":
        return cls(
            original_url=result.original_url,
            variants_tried=[FetchLogModel.from_dataclass(log) for log in result.variants_tried],
            images=result.images,
        )


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


def build_headers() -> Dict[str, str]:
    headers = dict(HEADERS_BASE)
    headers["User-Agent"] = random.choice(USER_AGENTS)
    return headers


def build_proxies(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def extract_images(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: Set[str] = set()

    def push(url: Optional[str]) -> None:
        if not url:
            return
        cleaned = url.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        found.append(cleaned)

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

    return found


def fetch_html(url: str, proxy_url: Optional[str], timeout: float) -> Tuple[Optional[str], FetchLog]:
    headers = build_headers()
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


def scrape_images(url: str, *, proxy: Optional[str], include_mobile: bool, max_depth: int, delay: float, timeout: float) -> ScrapeResult:
    normalized = normalize_url(url)
    variants = candidate_variants(normalized, include_mobile=include_mobile, max_depth=max_depth)
    logs: List[FetchLog] = []
    images: List[str] = []

    for candidate in variants:
        html, log_entry = fetch_html(candidate, proxy, timeout)
        logs.append(log_entry)
        if html:
            extracted = extract_images(html)
            for img_url in extracted:
                if img_url not in images:
                    images.append(img_url)
        time.sleep(delay)
    return ScrapeResult(normalized, logs, images)


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
            include_mobile=payload.include_mobile,
            max_depth=payload.max_depth,
            delay=delay,
            timeout=timeout,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ScrapeResultModel.from_dataclass(result)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe de imágenes para enlaces públicos de Facebook")
    parser.add_argument("--url", required=True, help="URL pública de Facebook (post, share, etc.)")
    parser.add_argument("--proxy", default=FACEBOOK_PROXY_URL, help="Proxy HTTP/S opcional")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout por request (s)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Delay entre intentos (s)")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Máximo de variantes a probar")
    parser.add_argument("--no-mobile", action="store_true", help="No probar variantes m./mbasic")
    parser.add_argument("--json", action="store_true", help="Imprimir resultado en JSON en lugar de tabla")
    args = parser.parse_args()

    result = scrape_images(
        args.url,
        proxy=args.proxy,
        include_mobile=not args.no_mobile,
        max_depth=args.max_depth,
        delay=args.delay,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    else:
        dump_table(result)


if __name__ == "__main__":
    main()
