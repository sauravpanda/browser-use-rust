import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def _load_google_module():
    google_pkg = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    gtypes = types.ModuleType("google.genai.types")

    class MediaResolution:
        MEDIA_RESOLUTION_UNSPECIFIED = "unspecified"
        MEDIA_RESOLUTION_LOW = "low"
        MEDIA_RESOLUTION_MEDIUM = "medium"
        MEDIA_RESOLUTION_HIGH = "high"
        MEDIA_RESOLUTION_ULTRA_HIGH = "ultra_high"

    class GenerateContentConfig:
        last_kwargs = None

        def __init__(self, **kwargs):
            type(self).last_kwargs = dict(kwargs)
            self.kwargs = dict(kwargs)

    class Tool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FunctionDeclaration:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Part:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.text = kwargs.get("text")
            self.function_call = kwargs.get("function_call")

    class Content:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Blob:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FunctionCall:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FunctionResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ThinkingConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ThinkingLevel:
        def __init__(self, value):
            self.value = value

    gtypes.MediaResolution = MediaResolution
    gtypes.GenerateContentConfig = GenerateContentConfig
    gtypes.Tool = Tool
    gtypes.FunctionDeclaration = FunctionDeclaration
    gtypes.Part = Part
    gtypes.Content = Content
    gtypes.Blob = Blob
    gtypes.FunctionCall = FunctionCall
    gtypes.FunctionResponse = FunctionResponse
    gtypes.ThinkingConfig = ThinkingConfig
    gtypes.ThinkingLevel = ThinkingLevel

    class Client:
        pass

    genai_mod.Client = Client
    genai_mod.types = gtypes
    google_pkg.genai = genai_mod

    old_modules = {
        name: sys.modules.get(name)
        for name in ("google", "google.genai", "google.genai.types")
    }
    sys.modules["google"] = google_pkg
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = gtypes
    try:
        spec = importlib.util.spec_from_file_location(
            "google_llm_under_test",
            ROOT / "python/browser_use_rs/llm/google.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, gtypes
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class FakeModels:
    def __init__(self):
        self.config = None

    async def generate_content(self, *, model, contents, config):
        self.config = config
        usage = types.SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=7,
            thoughts_token_count=3,
            cached_content_token_count=11,
            prompt_tokens_details=[
                types.SimpleNamespace(modality="IMAGE", token_count=64),
                types.SimpleNamespace(modality="TEXT", token_count=36),
            ],
            candidates_tokens_details=[
                types.SimpleNamespace(modality="TEXT", token_count=7),
            ],
        )
        candidate = types.SimpleNamespace(
            content=types.SimpleNamespace(parts=[]),
        )
        return types.SimpleNamespace(
            candidates=[candidate],
            usage_metadata=usage,
        )


class FakeClient:
    def __init__(self):
        self.aio = types.SimpleNamespace(models=FakeModels())


class GoogleMediaResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_defaults_to_low_media_resolution_and_records_details(self):
        module, _gtypes = _load_google_module()
        client = FakeClient()

        llm = module.ChatGoogle(model="gemini-2.5-flash", client=client)
        completion = await llm.ainvoke([], [], system="system")

        self.assertEqual("low", client.aio.models.config.kwargs["media_resolution"])
        self.assertEqual(100, completion.usage.input)
        self.assertEqual(10, completion.usage.output)
        self.assertEqual({"image": 64, "text": 36}, completion.usage.input_details)
        self.assertEqual({"text": 7}, completion.usage.output_details)
        dumped = completion.usage.model_dump()
        self.assertEqual(64, dumped["input_image_tokens"])
        self.assertEqual(36, dumped["input_text_tokens"])

    def test_media_resolution_can_be_overridden_or_disabled(self):
        module, _gtypes = _load_google_module()

        llm = module.ChatGoogle(
            model="gemini-2.5-flash",
            media_resolution="high",
            client=FakeClient(),
        )
        self.assertEqual("high", llm.media_resolution)

        llm.set_media_resolution("auto")
        self.assertIsNone(llm.media_resolution)


if __name__ == "__main__":
    unittest.main()
