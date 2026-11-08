from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

from translators.base import BaseTranslator
from translators.batch import translate_with_markers
from translators.errors import TranslationServiceUnavailable

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_GOOGLE_ENDPOINTS = (
    "https://translate.googleapis.com/translate_a/single",
    "https://translate.google.com/translate_a/single",
    "https://clients5.google.com/translate_a/t",
)


class GoogleUnavailableError(TranslationServiceUnavailable):
    pass


_breaker_lock = threading.Lock()
_breaker_until = 0.0
_breaker_reason = ""


def _reset_breaker() -> None:
    global _breaker_until, _breaker_reason
    with _breaker_lock:
        _breaker_until = 0.0
        _breaker_reason = ""


def map_google_language(language: str) -> str:
    lang = (language or "auto").lower()
    if lang in {"zh", "zh-cn", "zh-hans"}:
        return "zh-CN"
    if lang in {"zh-tw", "zh-hant"}:
        return "zh-TW"
    return language or "auto"


def parse_google_response(payload: Any) -> str:
    """Parse both google_a/single and clients5 response shapes."""

    if not isinstance(payload, list) or not payload:
        return ""
    first = payload[0]
    if isinstance(first, list):
        if first and isinstance(first[0], list):
            return "".join(
                segment[0]
                for segment in first
                if isinstance(segment, list)
                and segment
                and isinstance(segment[0], str)
            )
        strings = [item for item in first if isinstance(item, str) and item]
        if strings:
            return strings[-1]
    if isinstance(first, str):
        return first
    return ""


class GoogleTranslator(BaseTranslator):
    """Free Google web translator (no API key; unofficial endpoint)."""

    name = "google"
    envs = {"GOOGLE_TRANSLATE_ENDPOINT": None}

    def __init__(self, lang_in="auto", lang_out="zh", model=""):
        super().__init__(lang_in, lang_out, model)
        override = os.environ.get("GOOGLE_TRANSLATE_ENDPOINT")
        self.endpoints = [override] if override else []
        self.endpoints.extend(
            endpoint
            for endpoint in _GOOGLE_ENDPOINTS
            if endpoint not in self.endpoints
        )
        self.last_usage = {}

    def do_translate(self, original_text: str) -> str:
        global _breaker_until, _breaker_reason
        if not original_text.strip():
            return ""

        with _breaker_lock:
            if time.monotonic() < _breaker_until:
                raise GoogleUnavailableError(_breaker_reason)

        source = "auto" if self.lang_in in {"", "auto", "auto-detect"} else map_google_language(self.lang_in)
        target = map_google_language(self.lang_out)
        last_error = ""

        for endpoint in self.endpoints:
            params = {
                "client": "gtx",
                "sl": source,
                "tl": target,
                "dt": "t",
                "q": original_text,
            }
            if "clients5" in endpoint:
                params = {
                    "client": "dict-chrome-ex",
                    "sl": source,
                    "tl": target,
                    "q": original_text,
                }
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers={"User-Agent": _UA, "Accept": "application/json"},
                    timeout=(3, 10),
                )
            except requests.RequestException as exc:
                last_error = f"{endpoint}: {type(exc).__name__}"
                continue

            if response.status_code != 200:
                last_error = f"{endpoint}: HTTP {response.status_code}"
                continue
            try:
                translated = parse_google_response(response.json())
            except ValueError:
                last_error = f"{endpoint}: invalid JSON"
                continue
            if translated:
                _reset_breaker()
                self.last_usage = {}
                return translated
            last_error = f"{endpoint}: empty translation"

        message = (
            "Google free translation failed "
            f"({last_error}). It may be blocked by the network; "
            "set HTTPS_PROXY or GOOGLE_TRANSLATE_ENDPOINT."
        )
        with _breaker_lock:
            _breaker_until = time.monotonic() + 300.0
            _breaker_reason = message
        raise GoogleUnavailableError(message)

    def translate_batch(self, texts: list[str]) -> list[str | None]:
        return translate_with_markers(self, texts)
