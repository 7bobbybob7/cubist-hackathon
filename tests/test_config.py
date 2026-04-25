import yaml

from framework.config import DEFAULT_CONFIG, load_config


def test_load_config_returns_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg["budget"]["daily_cap_usd"] == 50.00
    assert cfg["models"]["sonnet"] == "claude-sonnet-4-6"
    assert "claude-opus-4-7" in cfg["pricing"]


def test_load_config_deep_merges(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "budget": {"daily_cap_usd": 1.50},
        "pricing": {"claude-opus-4-7": {"input": 99.0, "output": 999.0}},
    }))
    cfg = load_config(p)
    # overridden
    assert cfg["budget"]["daily_cap_usd"] == 1.50
    assert cfg["pricing"]["claude-opus-4-7"] == {"input": 99.0, "output": 999.0}
    # untouched defaults still present
    assert cfg["models"]["sonnet"] == DEFAULT_CONFIG["models"]["sonnet"]
    assert cfg["pricing"]["claude-haiku-4-5"]["input"] == 1.00


def test_pricing_table_covers_methodology_models():
    for m in ("claude-opus-4-7", "claude-sonnet-4-6",
              "claude-haiku-4-5-20251001"):
        assert m in DEFAULT_CONFIG["pricing"], m
