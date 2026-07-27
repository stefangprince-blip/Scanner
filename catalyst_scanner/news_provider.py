"""Compliant, replaceable symbol-news provider layer.

Yahoo's public RSS feed is used as the preferred association record. No page
scraping or private/undocumented JSON endpoint is used.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import requests

from . import config, rssparse


@dataclass(frozen=True)
class SecurityIdentity:
    symbol: str
    exchange: str = ""
    company_name: str = ""
    previous_symbols: tuple[str, ...] = ()
    cik: str = ""
    provider_ids: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def cache_key(self) -> tuple:
        return (
            self.symbol.upper(), self.exchange.upper(), self.company_name.lower(),
            self.previous_symbols, self.cik, self.provider_ids,
        )


def normalize_yahoo_symbol(symbol: str, exchange: str = "") -> str:
    value = str(symbol or "").strip().upper()
    value = re.sub(r"\s+", "", value)
    # Yahoo represents class-share separators with a hyphen.
    value = re.sub(r"(?<=[A-Z])\.(?=[A-Z]$)", "-", value)
    return value


def yahoo_symbol_page_url(identity: SecurityIdentity) -> str:
    symbol = normalize_yahoo_symbol(identity.symbol, identity.exchange)
    return f"https://finance.yahoo.com/quote/{quote(symbol, safe='-^=')}/news"


def _publisher_from_url(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return "Unknown publisher"
    known = {
        "sec.gov": "SEC", "fda.gov": "FDA", "businesswire.com": "Business Wire",
        "globenewswire.com": "GlobeNewswire", "prnewswire.com": "PR Newswire",
        "reuters.com": "Reuters", "finance.yahoo.com": "Yahoo Finance",
    }
    for domain, publisher in known.items():
        if host == domain or host.endswith("." + domain):
            return publisher
    return host or "Unknown publisher"


class SymbolNewsProvider:
    def get_quick_news(self, identity: SecurityIdentity) -> dict | None:
        raise NotImplementedError

    def get_latest_articles(self, identity: SecurityIdentity, limit: int) -> dict:
        raise NotImplementedError

    def get_symbol_news_page_url(self, identity: SecurityIdentity) -> str:
        return yahoo_symbol_page_url(identity)


class YahooRssSymbolNewsProvider(SymbolNewsProvider):
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _articles(self, identity: SecurityIdentity, limit: int) -> list[dict]:
        symbol = normalize_yahoo_symbol(identity.symbol, identity.exchange)
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={quote(symbol)}&region=US&lang=en-US"
        )
        response = self.session.get(
            url, headers={"User-Agent": config.USER_AGENT}, timeout=8
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        elif int(getattr(response, "status_code", 500)) >= 400:
            raise requests.HTTPError(str(response.status_code))
        found = []
        for entry in rssparse.parse(response.content)[: max(limit * 4, 12)]:
            article_url = str(entry.get("link") or "")
            published = float(entry.get("published") or 0)
            publisher = str(entry.get("source") or "").strip() or _publisher_from_url(article_url)
            if publisher == "Yahoo Finance":
                publisher = "Publisher unavailable"
            found.append({
                "headline": entry.get("title", ""),
                "url": article_url,
                "source": publisher,
                "originalPublisher": publisher,
                "published": published or None,
                "publicationTimeAvailable": bool(
                    entry.get("publication_time_available") and published
                ),
                "first_seen": time.time(),
                "body": entry.get("summary", ""),
                "score": 0, "tags": [], "id": entry.get("id", ""),
                "discoverySource": "yahoo_finance",
                "yahooFinanceUrl": yahoo_symbol_page_url(identity),
                "timestampQuality": (
                    "yahoo_displayed"
                    if entry.get("publication_time_available") and published
                    else "discovered_fallback"
                ),
                "discoveredAt": time.time(),
            })
            if len(found) >= limit:
                break
        return found

    def get_quick_news(self, identity: SecurityIdentity) -> dict | None:
        try:
            articles = self._articles(identity, 1)
            return articles[0] if articles else None
        except requests.RequestException:
            return None

    def get_latest_articles(self, identity: SecurityIdentity, limit: int) -> dict:
        try:
            articles = self._articles(identity, limit)
            return {"articles": articles, "sourcesChecked": ["yahoo_finance"], "providerError": False}
        except requests.RequestException:
            return {"articles": [], "sourcesChecked": ["yahoo_finance"], "providerError": True}


class CompositeSymbolNewsProvider(SymbolNewsProvider):
    def __init__(self, store, yahoo: SymbolNewsProvider | None = None):
        self.store = store
        self.yahoo = yahoo or YahooRssSymbolNewsProvider()

    def get_quick_news(self, identity: SecurityIdentity) -> dict | None:
        yahoo = self.yahoo.get_quick_news(identity)
        if yahoo:
            return yahoo
        stored = self.store.latest_news(identity.symbol, limit=8)
        return next((row for row in stored if str(row.get("url") or "").startswith(("http://", "https://"))), None)

    def get_latest_articles(self, identity: SecurityIdentity, limit: int) -> dict:
        yahoo_result = self.yahoo.get_latest_articles(identity, limit)
        articles = list(yahoo_result.get("articles") or [])
        checked = list(yahoo_result.get("sourcesChecked") or [])
        checked += ["company_ir", "sec", "government", "licensed_feeds"]
        for stored in self.store.latest_news(identity.symbol, limit=24):
            source = str(stored.get("source") or "Unknown publisher")
            lowered = source.lower()
            discovery = (
                "sec" if "sec" in lowered
                else "fda" if "fda" in lowered or "government" in lowered
                else "company_ir" if "investor relation" in lowered or "company" in lowered
                else "licensed_feed"
            )
            articles.append({
                **stored,
                "originalPublisher": source,
                "discoverySource": discovery,
                "timestampQuality": (
                    "primary_source"
                    if discovery in {"sec", "fda", "company_ir"}
                    else "provider"
                ),
                "discoveredAt": stored.get("first_seen"),
            })
        return {
            "articles": articles,
            "sourcesChecked": checked,
            "providerError": bool(yahoo_result.get("providerError")),
            "yahooFinanceUrl": yahoo_symbol_page_url(identity),
        }
