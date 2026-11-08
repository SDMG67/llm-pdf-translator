from translators.base import BaseTranslator
from translators.bing import BingTranslator
from translators.mock import MockTranslator, EchoTranslator, UpperTranslator
from translators.deepseek import DeepseekTranslator
from translators.google import GoogleTranslator
from translators.openai import OpenAITranslator

TRANSLATOR_REGISTRY: dict[str, type[BaseTranslator]] = {
    MockTranslator.name: MockTranslator,
    EchoTranslator.name: EchoTranslator,
    UpperTranslator.name: UpperTranslator,
    DeepseekTranslator.name: DeepseekTranslator,
    OpenAITranslator.name: OpenAITranslator,
    GoogleTranslator.name: GoogleTranslator,
    BingTranslator.name: BingTranslator,
}


def get_translator(
    name: str, lang_in: str = "auto", lang_out: str = "zh", model: str = ""
) -> BaseTranslator:
    cls = TRANSLATOR_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown engine {name!r}, available: {sorted(TRANSLATOR_REGISTRY)}"
        )
    return cls(lang_in=lang_in, lang_out=lang_out, model=model)
