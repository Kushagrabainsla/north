"""How a written model spec is matched against a live catalog entry.

Pure helpers, exercised directly: the chain's pinned model and the user's manual
model choice both resolve through ``model_matches``, so a change here changes
which model answers.
"""

from __future__ import annotations

from inference.model_policy import model_matches, split_spec

# ---------------------------------------------------------------- pure helpers


def test_model_matches_family():
    # A family token matches version-suffixed ids (contiguous substring)...
    assert model_matches("claude-sonnet", "openrouter", "anthropic/claude-sonnet-5")
    assert model_matches("claude-sonnet", "openrouter", "anthropic/claude-sonnet-4.6")
    # ...matches across provider id schemes (openrouter vs the gemini direct api)...
    assert model_matches("gemini-2.5-pro", "openrouter", "google/gemini-2.5-pro")
    assert model_matches("gemini-2.5-pro", "gemini", "models/gemini-2.5-pro")
    # ...but does NOT match a different family.
    assert not model_matches("claude-sonnet", "openrouter", "anthropic/claude-3.5-haiku")
    assert not model_matches("gemini-2.5-pro", "gemini", "models/gemini-2.5-flash")


def test_model_matches_rejects_numeric_overmatch():
    # The critical bug this guards: an independent-words match wrongly accepted
    # gpt-3.5-turbo for "gpt-5" (because "5" occurs in "3.5") and nemotron-49b for
    # "llama-4". Contiguous matching rejects both.
    assert not model_matches("gpt-5", "openrouter", "openai/gpt-3.5-turbo-0613")
    assert model_matches("gpt-5", "openrouter", "openai/gpt-5.6-luna")
    assert not model_matches("llama-4-scout", "openrouter", "nvidia/llama-3.3-nemotron-super-49b")
    assert model_matches("llama-4-scout", "groq", "meta-llama/llama-4-scout-17b-16e-instruct")


def test_model_matches_provider_qualifier():
    assert model_matches("groq:llama-3.3", "groq", "llama-3.3-70b-versatile")
    # Provider qualifier must match the provider name.
    assert not model_matches("groq:llama-3.3", "openrouter", "meta/llama-3.3-70b")


def test_split_spec():
    assert split_spec("openrouter:anthropic/claude-sonnet") == ("openrouter", "anthropic/claude-sonnet")
    # A bare OpenRouter id (contains '/') is NOT treated as provider-qualified.
    assert split_spec("anthropic/claude-sonnet") == (None, "anthropic/claude-sonnet")
    assert split_spec("gpt-4o") == (None, "gpt-4o")
