import re

from translators.batch import translate_with_markers


class MarkerEngine:
    def __init__(self, drop_markers: bool = False):
        self.calls = 0
        self.drop_markers = drop_markers

    def translate(self, text: str) -> str:
        self.calls += 1
        if self.drop_markers:
            return "译:" + re.sub(r"<<<BZSEG_[^>]+>>>", "", text)
        if "<<<BZSEG_" not in text:
            return "译:" + text

        pattern = re.compile(r"<<<BZSEG_[a-f0-9]+_\d+>>>\n")
        matches = list(pattern.finditer(text))
        parts = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            parts.append(match.group(0) + "译:" + text[match.end() : end].strip())
        return "\n\n".join(parts)


def test_batch_uses_one_marker_request_when_preserved():
    engine = MarkerEngine()
    result = translate_with_markers(engine, ["alpha", "beta", "gamma"])
    assert result == ["译:alpha", "译:beta", "译:gamma"]
    assert engine.calls == 1


def test_batch_falls_back_per_item_when_markers_are_lost():
    engine = MarkerEngine(drop_markers=True)
    result = translate_with_markers(engine, ["alpha", "beta", "gamma"])
    assert result == ["译:alpha", "译:beta", "译:gamma"]
    assert engine.calls == 4
