from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from translators.base import BaseTranslator
from translators.batch import translate_with_markers

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
_HOSTS = ("https://cn.bing.com", "https://www.bing.com")
_IG_RE = re.compile(r'IG:"([^"]+)"')
_ABUSE_RE = re.compile(
    r"params_AbusePreventionHelper\s*=\s*(\[[^\]]+\])",
    re.S,
)
_IID_RE = re.compile(r'data-iid="(translator\.\d+)"')


@dataclass
class _BingContext:
    host: str
    session: requests.Session
    ig: str
    key: str
    token: str
    iids: list[str]
    expires_at: float


_thread_local = threading.local()


def map_bing_language(language: str) -> str:
    lang = (language or "auto").lower()
    if lang in {"zh", "zh-cn", "zh-hans"}:
        return "zh-Hans"
    if lang in {"zh-tw", "zh-hant"}:
        return "zh-Hant"
    return language or "auto"


def parse_bing_response(payload: Any) -> str:
    if not isinstance(payload, list):
        return ""
    for item in payload:
        if not isinstance(item, dict):
            continue
        translations = item.get("translations")
        if isinstance(translations, list):
            texts = [
                entry.get("text", "")
                for entry in translations
                if isinstance(entry, dict) and entry.get("text")
            ]
            if texts:
                return "".join(texts)
    return ""


def _parse_page(html: str):
    ig = _IG_RE.search(html)
    abuse = _ABUSE_RE.search(html)
    if not ig or not abuse:
        return None
    try:
        key, token, ttl_ms = json.loads(abuse.group(1))[:3]
    except (TypeError, ValueError):
        return None
    iids = sorted(set(_IID_RE.findall(html)))
    for fallback in ("translator.5023", "translator.5028"):
        if fallback not in iids:
            iids.append(fallback)
    try:
        ttl_seconds = max(60.0, float(ttl_ms) / 1000.0)
    except (TypeError, ValueError):
        ttl_seconds = 900.0
    return str(ig.group(1)), str(key), str(token), iids, ttl_seconds


def _fetch_context(force: bool = False) -> _BingContext:
    context = getattr(_thread_local, "context", None)
    if context is not None and not force and time.time() < context.expires_at:
        return context

    errors = []
    configured = os.environ.get("BING_TRANSLATOR_HOST")
    hosts = [configured] if configured else []
    hosts.extend(host for host in _HOSTS if host not in hosts)

    for host in hosts:
        session = requests.Session()
        session.headers.update(
            {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
        )
        try:
            page = session.get(f"{host}/translator", timeout=15)
            page.raise_for_status()
            parsed = _parse_page(page.text)
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}")
            continue
        if not parsed:
            errors.append(f"{host}: page parameters missing")
            continue

        ig, key, token, iids, ttl_seconds = parsed
        context = _BingContext(
            host=host,
            session=session,
            ig=ig,
            key=key,
            token=token,
            iids=iids,
            # Refresh a little before the page token expires.
            expires_at=time.time() + min(ttl_seconds, 1800.0) - 60.0,
        )
        _thread_local.context = context
        return context

    raise RuntimeError(
        "Bing free translation unavailable: " + "; ".join(errors)
    )


def _invalidate_context() -> None:
    _thread_local.context = None


class BingTranslator(BaseTranslator):
    """Free Bing web translator (no API key; unofficial endpoint)."""

    name = "bing"
    envs = {"BING_TRANSLATOR_HOST": None}

    def __init__(self, lang_in="auto", lang_out="zh", model=""):
        super().__init__(lang_in, lang_out, model)
        self.last_usage = {}

    def do_translate(self, original_text: str) -> str:
        if not original_text.strip():
            return ""

        source = (
            "auto-detect"
            if self.lang_in in {"", "auto", "auto-detect"}
            else map_bing_language(self.lang_in)
        )
        target = map_bing_language(self.lang_out)
        last_error = ""

        for attempt in range(2):
            context = _fetch_context(force=attempt > 0)
            for iid in list(context.iids):
                try:
                    response = context.session.post(
                        f"{context.host}/ttranslatev3",
                        params={
                            "isVertical": "1",
                            "IG": context.ig,
                            "IID": iid,
                            "token": context.token,
                            "key": context.key,
                        },
                        data={
                            "fromLang": source,
                            "to": target,
                            "text": original_text,
                        },
                        headers={
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{context.host}/translator",
                            "Origin": context.host,
                        },
                        timeout=20,
                    )
                except requests.RequestException as exc:
                    last_error = type(exc).__name__
                    continue

                if response.status_code != 200 or not response.text.strip():
                    last_error = f"HTTP {response.status_code}, empty body"
                    continue
                try:
                    translated = parse_bing_response(response.json())
                except ValueError:
                    last_error = "invalid JSON"
                    continue
                if translated:
                    context.iids.remove(iid)
                    context.iids.insert(0, iid)
                    self.last_usage = {}
                    return translated
                last_error = "empty translation"

            _invalidate_context()

        raise RuntimeError(
            "Bing free translation failed "
            f"({last_error}); the public endpoint may be rate-limited or changed."
        )

    def translate_batch(self, texts: list[str]) -> list[str | None]:
        return translate_with_markers(self, texts)
