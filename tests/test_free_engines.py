from translators import TRANSLATOR_REGISTRY
from translators.bing import map_bing_language, parse_bing_response
from translators.google import map_google_language, parse_google_response


def test_free_engines_are_registered():
    assert {"bing", "google"} <= set(TRANSLATOR_REGISTRY)


def test_google_response_shapes():
    segments = [[["你好，世界", "Hello world", None, None, 10]], None, "en"]
    assert parse_google_response(segments) == "你好，世界"
    assert parse_google_response([["Hello world", "你好"]]) == "你好"
    assert parse_google_response(["你好"]) == "你好"


def test_bing_response_shape():
    payload = [
        {
            "translations": [{"text": "你好，世界", "to": "zh-Hans"}],
            "detectedLanguage": {"language": "en"},
        }
    ]
    assert parse_bing_response(payload) == "你好，世界"


def test_free_engine_language_mapping():
    assert map_google_language("zh") == "zh-CN"
    assert map_google_language("zh-tw") == "zh-TW"
    assert map_bing_language("zh") == "zh-Hans"
    assert map_bing_language("zh-tw") == "zh-Hant"
