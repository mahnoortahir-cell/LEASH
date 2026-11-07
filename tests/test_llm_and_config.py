from types import SimpleNamespace

import pytest

from leash import config
from leash.llm import DEFAULT_MODEL, AnthropicLLM, FakeLLM, extract_json, get_llm
from leash.paypal_api import PayPalClient


def test_extract_json_finds_the_object_in_chatty_replies():
    assert extract_json('Sure! {"a": 1, "b": [2]} Done.') == {"a": 1, "b": [2]}
    for bad in ("no json here", "{broken", "[1,2,3]", ""):
        with pytest.raises(ValueError):
            extract_json(bad)


def test_get_llm_is_none_without_a_key_and_a_client_with_one(monkeypatch):
    assert get_llm() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    llm = get_llm()
    assert isinstance(llm, AnthropicLLM) and llm.model == DEFAULT_MODEL
    monkeypatch.setenv("LEASH_MODEL", "some-other-model")
    assert get_llm().model == "some-other-model"


def test_anthropic_adapter_sends_the_documented_arguments_and_joins_text_blocks():
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Hello "), SimpleNamespace(type="tool_use", text="IGNORED"),
                                        SimpleNamespace(type="text", text="world")])

    llm = AnthropicLLM(api_key="sk-test-not-real")
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=create))  # no network: a stand-in client
    assert llm.complete("be brief", "hi", max_tokens=123) == "Hello world"
    assert seen == {"model": DEFAULT_MODEL, "max_tokens": 123, "system": "be brief",
                    "messages": [{"role": "user", "content": "hi"}]}


def test_fake_llm_records_calls_and_runs_out_gracefully():
    llm = FakeLLM(["one"])
    assert llm.complete("s", "u") == "one" and llm.complete("s", "u") == "" and len(llm.calls) == 2


def test_env_file_is_loaded_without_overriding_real_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nPAYPAL_CLIENT_ID="abc"\nPAYPAL_CLIENT_SECRET=def\nEMPTY=\nBAD LINE\nKEEP=from_file\n')
    monkeypatch.setenv("KEEP", "from_shell")
    for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    config.load_env(str(env))
    import os
    assert os.environ["PAYPAL_CLIENT_ID"] == "abc" and os.environ["PAYPAL_CLIENT_SECRET"] == "def"
    assert os.environ["KEEP"] == "from_shell"
    monkeypatch.delenv("EMPTY", raising=False)


def test_empty_settings_fall_back_to_the_sandbox(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PAYPAL_BASE_URL", "")
    client = config.paypal_client()
    assert client.base_url == PayPalClient.SANDBOX_URL and client.is_sandbox


def test_merchants_file_formats(tmp_path):
    f = tmp_path / "m.json"
    f.write_text('{"A Shop": "a@x.com", "B Shop": ["b@x.com", "MID2"]}')
    assert config.load_merchants(str(f)) == {"A Shop": ["a@x.com"], "B Shop": ["b@x.com", "MID2"]}
    with pytest.raises(SystemExit, match="merchants.example.json"):
        config.load_merchants(str(tmp_path / "missing.json"))
