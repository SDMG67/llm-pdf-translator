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


class DeepseekTranslator(BaseTranslator):
    name = "deepseek"
    envs = {
        "DEEPSEEK_API_KEY": None,
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-chat",
    }

    def __init__(self, lang_in="auto", lang_out="zh", model=""):
        super().__init__(lang_in, lang_out, model)
        # Dedicated DEEPSEEK_* vars, falling back to shared OPENAI_* for convenience.
        self.api_key = os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = (
            os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        )
        self.model = (
            model
            or os.environ.get("DEEPSEEK_MODEL")
            or "deepseek-chat"
        )
        self.temperature = 0.3

    def _build_llm(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=4096,
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
            raise ValueError("DEEPSEEK_API_KEY / OPENAI_API_KEY is not set")
        llm = self._build_llm()
        messages = [
            (
                "system",
                "You are a professional translator. Translate the user input into "
                + self.lang_out
                + ". Output the translation only, no explanations.",
            ),
            ("human", original_text),
        ]
        response = self._invoke(llm, messages)
        self.last_usage = getattr(response, "usage_metadata", None) or {}

        content = response.content
        # Some providers return a list of content parts instead of a plain string.
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return content