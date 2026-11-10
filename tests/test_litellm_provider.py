"""Tests for the OpenAI-compatible (LiteLLM) provider.

The live tests call a REAL LiteLLM proxy, never a stub, and skip when these
env vars are not set:
  AI_COMMIT_TEST_LITELLM_URL   e.g. https://litellm-vertex.hankel.ai
  AI_COMMIT_API_KEY            a key allowed to use the model below
  AI_COMMIT_TEST_LITELLM_MODEL default claude-haiku-4-5

Headless: imports only ai_commit_core (no Dear PyGui).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core

URL = os.environ.get("AI_COMMIT_TEST_LITELLM_URL", "")
KEY = os.environ.get("AI_COMMIT_API_KEY", "")
MODEL = os.environ.get("AI_COMMIT_TEST_LITELLM_MODEL", "claude-haiku-4-5")

DIFF = """diff --git a/greet.py b/greet.py
--- a/greet.py
+++ b/greet.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return "hi " + name
+    return f"Hello, {name}!"
"""


def _live():
    if not (URL and KEY):
        print("skip (set AI_COMMIT_TEST_LITELLM_URL and AI_COMMIT_API_KEY)")
        return False
    return True


def test_chat_url_with_and_without_v1():
    f = ai_commit_core.litellm_chat_url
    assert f("https://h") == "https://h/v1/chat/completions"
    assert f("https://h/") == "https://h/v1/chat/completions"
    assert f("https://h/v1") == "https://h/v1/chat/completions"
    assert f("https://h/v1/") == "https://h/v1/chat/completions"


def test_litellm_is_an_ollama_error():
    # Every existing `except OllamaError` path must also catch LiteLLM failures.
    assert issubclass(ai_commit_core.LiteLLMError, ai_commit_core.OllamaError)


def test_key_falls_back_to_windows_user_env_when_process_env_is_stale():
    # Reads the REAL HKCU\Environment value, the way a GUI launched before
    # the variable existed has to.
    if os.name != "nt":
        print("skip (Windows only)")
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            stored, _ = winreg.QueryValueEx(reg, "AI_COMMIT_API_KEY")
    except OSError:
        print("skip (no AI_COMMIT_API_KEY user env var)")
        return
    saved = os.environ.pop("AI_COMMIT_API_KEY", None)
    try:
        assert ai_commit_core.resolve_litellm_api_key("") == stored
        assert ai_commit_core.resolve_litellm_api_key("explicit") == "explicit"
    finally:
        if saved is not None:
            os.environ["AI_COMMIT_API_KEY"] = saved


def test_live_generate_via_dispatch():
    if not _live():
        return
    msg = ai_commit_core.generate_message(
        DIFF, {"provider": "litellm", "model": MODEL, "url": URL, "api_key": KEY},
    )
    assert msg.strip(), "empty commit message"
    print(f"    first line: {msg.splitlines()[0]!r}")


def test_live_bad_key_is_a_clear_error():
    if not _live():
        return
    try:
        ai_commit_core.generate_message_litellm(DIFF, MODEL, URL, api_key="sk-not-a-real-key")
    except ai_commit_core.LiteLLMError as exc:
        assert "API key" in str(exc), str(exc)
    else:
        raise AssertionError("a bad key was accepted")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
