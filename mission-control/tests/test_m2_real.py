"""M2: real-model panel is declared config and swaps in with no harness change;
the harness degrades gracefully when keys are absent."""

from models.judges import NIM_CATALOG, PANEL, build_real_judges
from workers.claude_worker import build_real_worker, parse_json_loose

KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "OLLAMA_HOST", "MAX_JUDGES"]


def _clear_keys(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_panel_is_three_distinct_providers():
    providers = [j.provider for j in PANEL]
    assert set(providers) == {"anthropic", "openai", "nvidia"}
    # NVIDIA NIM is served via the OpenAI-compatible endpoint.
    nim = [j for j in PANEL if j.provider == "nvidia"][0]
    assert nim.base_url == "https://integrate.api.nvidia.com/v1"


def test_no_judges_without_keys(monkeypatch):
    _clear_keys(monkeypatch)
    assert build_real_judges() == []


def test_one_judge_per_present_key(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-not-called")
    judges = build_real_judges()
    assert len(judges) == 1 and judges[0].name == "anthropic-claude"
    assert judges[0].available is True  # construction only; no network call


def test_faulty_grader_swaps_in_deterministic_faulty_judge(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    judges = build_real_judges(faulty_grader=True)
    assert "FAULTY" in judges[0].name


def test_build_real_worker_none_without_keys(monkeypatch):
    _clear_keys(monkeypatch)
    assert build_real_worker("writer") is None


def test_build_real_worker_picks_present_provider(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    w = build_real_worker("writer")
    assert w is not None and w.provider == "openai" and w.available is True


def test_one_nvidia_key_activates_the_nim_bunch(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("NVIDIA_API_KEY", "fake")
    judges = build_real_judges()
    assert len(judges) >= 8, "a single NVIDIA key should light up the whole NIM bunch"
    assert all(j.cfg.provider == "nvidia" for j in judges)
    # distinct models, all on the NIM endpoint
    assert len({j.cfg.model for j in judges}) == len(judges)
    assert all(j.cfg.base_url == "https://integrate.api.nvidia.com/v1" for j in judges)


def test_nim_catalog_spans_many_families():
    ids = " ".join(model for _, model, _ in NIM_CATALOG).lower()
    for family in ("deepseek", "mistral", "qwen", "gemma", "phi", "llama", "nemotron"):
        assert family in ids, f"expected a {family} model in the NIM catalog"


def test_max_judges_caps_the_panel(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("NVIDIA_API_KEY", "fake")
    monkeypatch.setenv("MAX_JUDGES", "3")
    assert len(build_real_judges()) == 3


def test_ollama_is_opt_in(monkeypatch):
    _clear_keys(monkeypatch)
    assert build_real_judges() == []                       # off by default
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434/v1")
    judges = build_real_judges()
    assert judges and all(j.cfg.provider == "ollama" for j in judges)


def test_parse_json_loose_strips_fences():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('here you go: {"text": "hi"} thanks') == {"text": "hi"}
