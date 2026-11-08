from __future__ import annotations

from typing import Sequence

from rate_limiter import get_rate_limiter


class BaseTranslator:
    name: str = "base"
    envs: dict[str, str | None] = {}

    def __init__(
        self,
        lang_in: str = "auto",
        lang_out: str = "zh",
        model: str = "",
        qps: float = 4.0,
    ) -> None:
        # Share ONE limiter per engine name across instances within the worker
        # process. A fresh limiter per instance would reset the throttle every task.
        self.rate_limiter = get_rate_limiter(self.name, qps)
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.model = model

    def translate(self, original_text: str) -> str:
        """Single entry point: throttle first, then delegate to do_translate()."""
        self.rate_limiter.wait()
        return self.do_translate(original_text)

    def translate_batch(self, texts: Sequence[str]) -> list[str | None]:
        """Default batch behavior: preserve correctness by calling each item."""
        return [self.translate(text) for text in texts]

    def do_translate(self, original_text: str):
        raise NotImplementedError(f"{type(self).__name__} must implement do_translate()")

    def cache_params(self) -> dict:
        """Params that affect translation quality; they become part of the cache key."""
        return {
            "engine": self.name,
            "lang_in": self.lang_in,
            "lang_out": self.lang_out,
            "model": self.model,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} {self.lang_in}->{self.lang_out}>"
