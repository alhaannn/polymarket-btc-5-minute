"""
Polymarket Gamma & CLOB REST API client with retry and error-handling mechanisms.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

log = logging.getLogger("polybot.gamma")


class PolymarketClient:
    """HTTP client for Gamma API and CLOB REST with exponential backoff retries."""

    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        timeout: float = 8.0,
        max_retries: int = 4,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "polybot/0.1 (+https://github.com/polymarketbot)",
            "Accept": "application/json",
        })

        retries = Retry(
            total=max_retries,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("HTTP request failed [%s]: %s", url, e)
            raise

    def get_event(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch market event by slug."""
        try:
            data = self._get_json(f"{self.gamma_url}/events", {"slug": slug})
            if data and isinstance(data, list):
                return data[0]
            return None
        except Exception:
            return None

    def get_market(self, window_start: int) -> Optional[Dict[str, Any]]:
        """
        Return standard market info:
        {
            'slug': 'btc-updown-5m-...',
            'start': window_start,
            'end': window_start + 300,
            'tokens': {'Up': token_id, 'Down': token_id},
            'condition_id': ...,
            'question': ...
        }
        """
        slug = f"btc-updown-5m-{window_start}"
        ev = self.get_event(slug)
        if not ev or not ev.get("markets"):
            return None

        m = ev["markets"][0]
        outcomes_raw = m.get("outcomes", [])
        tokens_raw = m.get("clobTokenIds", [])

        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw

        if not outcomes or not tokens or len(outcomes) != len(tokens):
            return None

        tokens_map = dict(zip(outcomes, tokens))
        return {
            "slug": slug,
            "start": window_start,
            "end": window_start + 300,
            "tokens": tokens_map,
            "condition_id": m.get("conditionId"),
            "question": m.get("question"),
            "description": m.get("description"),
        }

    def get_orderbook(self, token_id: str) -> Dict[str, List[Tuple[float, float]]]:
        """
        Fetch order book via REST fallback:
        Returns:
            {'bids': [(price, size), ...], 'asks': [(price, size), ...]}
        """
        try:
            book = self._get_json(f"{self.clob_url}/book", {"token_id": token_id})
            bids = [(float(b["price"]), float(b["size"])) for b in book.get("bids", [])]
            asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
            return {
                "bids": sorted(bids, reverse=True),  # highest bid first
                "asks": sorted(asks),               # lowest ask first
            }
        except Exception as e:
            log.warning("Failed to fetch order book for token %s: %s", token_id, e)
            return {"bids": [], "asks": []}

    def get_winner(self, slug: str) -> Optional[str]:
        """
        Check outcome prices for resolution:
        Returns 'Up', 'Down', or None if unresolved.
        """
        try:
            ev = self.get_event(slug)
            if not ev or not ev.get("markets"):
                return None
            m = ev["markets"][0]
            prices_raw = m.get("outcomePrices")
            outcomes_raw = m.get("outcomes")
            if not prices_raw or not outcomes_raw:
                return None

            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

            float_prices = [float(p) for p in prices]
            if len(float_prices) >= 2 and max(float_prices) >= 0.99 and min(float_prices) <= 0.01:
                return outcomes[float_prices.index(max(float_prices))]
            return None
        except Exception as e:
            log.debug("Resolution check error for %s: %s", slug, e)
            return None
