import os
import logging

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from translators.base import BaseTranslator

logger = logging.getLogger(__name__)


class OpenAITranslator(BaseTranslator):
    name = "openai"
    envs = {
        "OPENAI_API_KEY": None,
        "OPENAI_BASE_URL": None,  # None -> official OpenAI endpoint
        "OPENAI_MODEL": "gpt-4o-mini",
    }

    def __init__(self, lang_in="auto", lang_out="zh", model=""):
        super().__init__(lang_in, lang_out, model)
        self.api_key = os.environ.get("OPENAI_API_KEY")
        # Only use OPENAI_BASE_URL if it looks like an OpenAI-compatible gateway.
        self.base_url = os.environ.get("OPENAI_BASE_URL") or None
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        self.temperature = 0.0

    def _build_llm(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            timeout=600,
        )

    @retry(
        retry=retry_if_exception_type((openai.RateLimitError, openai.APITimeoutError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _invoke(self, llm, messages):
        return llm.invoke(messages)

    def do_translate(self, original_text: str) -> str:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        llm = self._build_llm()
        messages = [
            "Translate the following text into "
            + self.lang_out
            + ". Output the translation only:\n\n"
            + original_text,
        ]
        response = self._invoke(llm, messages)
        self.last_usage = getattr(response, "usage_metadata", None) or {}
        content = response.content
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return content