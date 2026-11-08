from translators.base import BaseTranslator
class MockTranslator(BaseTranslator):
    name="mock"
    def do_translate(self, original_text: str)->str:
        return  f"[{self.lang_out}] {original_text}"
    
class EchoTranslator(BaseTranslator):
    name = "echo"

    def do_translate(self, text: str) -> str:
        return text


class UpperTranslator(BaseTranslator):
    name = "upper"

    def do_translate(self, text: str) -> str:
        return text.upper()