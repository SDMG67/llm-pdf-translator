from __future__ import annotations

import re
import secrets
from typing import Iterable

from translators.errors import TranslationServiceUnavailable


def chunk_indices(
    texts: list[str],
    max_items: int = 16,
    max_chars: int = 4000,
) -> Iterable[list[int]]:
    """Yield groups small enough for a web translator request."""

    current: list[int] = []
    size = 0
    for index, text in enumerate(texts):
        cost = len(text) + 40
        if current and (len(current) >= max_items or size + cost > max_chars):
            yield current
            current = []
            size = 0
        current.append(index)
        size += cost
    if current:
        yield current


def _parse_markers(text: str, token: str, count: int) -> list[str | None] | None:
    pattern = re.compile(rf"<<<BZSEG_{re.escape(token)}_(\d+)>>>")
    matches = list(pattern.finditer(text))
    if len(matches) != count:
        return None
    results: list[str | None] = [None] * count
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        index = int(match.group(1)) - 1
        if index < 0 or index >= count or results[index] is not None:
            return None
        results[index] = text[start:end].strip()
    return results


def translate_with_markers(
    engine,
    texts: list[str],
    max_items: int = 16,
    max_chars: int = 4000,
) -> list[str | None]:
    """Translate many blocks with one call, then fall back per block.

    The marker protocol is deliberately conservative. If an engine rewrites
    or drops markers, the fallback preserves correctness at the cost of more
    requests.
    """

    results: list[str | None] = [
        "" if not text.strip() else None for text in texts
    ]
    pending = [index for index, text in enumerate(texts) if text.strip()]
    if not pending:
        return results

    pending_texts = [texts[index] for index in pending]
    for local_chunk in chunk_indices(
        pending_texts,
        max_items=max_items,
        max_chars=max_chars,
    ):
        chunk = [pending[index] for index in local_chunk]
        token = secrets.token_hex(4)
        payload = "\n\n".join(
            f"<<<BZSEG_{token}_{position}>>>\n{texts[index]}"
            for position, index in enumerate(chunk, start=1)
        )

        parsed = None
        try:
            translated_payload = engine.translate(payload)
            parsed = _parse_markers(translated_payload, token, len(chunk))
        except TranslationServiceUnavailable:
            raise
        except Exception:
            parsed = None

        if parsed and all(value is not None and value.strip() for value in parsed):
            for index, value in zip(chunk, parsed):
                results[index] = value.strip()
            continue

        # Marker parsing failed or the batch response was empty: translate
        # each block separately so one malformed response cannot poison all.
        for index in chunk:
            try:
                value = engine.translate(texts[index])
                results[index] = value.strip() if value and value.strip() else None
            except TranslationServiceUnavailable:
                raise
            except Exception:
                results[index] = None
    return results
