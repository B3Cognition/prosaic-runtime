import pytest

from prosaic_runtime import RuntimeConfig, EndpointConfig


CONFIG = """default_profile: small
allowed_tools: [read_file]
routes:
  fast: small
profiles:
  small:
    base_url: http://localhost:8000/v1
    model: test-model
    features:
      streaming: false
"""


def test_provider_defaults_and_unknown_provider():
    assert EndpointConfig('http://localhost/v1', 'm').provider == 'openai-compatible'
    with pytest.raises(ValueError, match='provider'):
        EndpointConfig('http://localhost/v1', 'm', provider='unknown')


def test_native_provider_yaml_loading(tmp_path):
    path = tmp_path / 'native.yml'
    path.write_text(CONFIG.replace('    model: test-model', '    model: test-model\n    provider: anthropic'))
    assert RuntimeConfig.load(path).profiles['small'].provider == 'anthropic'


def test_anthropic_requires_positive_max_tokens():
    with pytest.raises(ValueError, match='max_tokens'):
        EndpointConfig('http://localhost/v1', 'm', provider='anthropic', max_tokens=None)


@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_load_yaml_configuration(tmp_path, suffix):
    path = tmp_path / f"custom.{suffix}"
    path.write_text(CONFIG)
    config = RuntimeConfig.load(path)
    assert config.default_profile == "small"
    assert config.routes == {"fast": "small"}
    assert config.allowed_tools == frozenset({"read_file"})
    assert config.profiles["small"].features["streaming"] is False


@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_default_configuration_discovery(tmp_path, monkeypatch, suffix):
    monkeypatch.chdir(tmp_path)
    (tmp_path / f"prosaic-runtime.{suffix}").write_text(CONFIG)
    assert RuntimeConfig.load().profiles["small"].model == "test-model"


def test_yaml_takes_precedence_over_yml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prosaic-runtime.yaml").write_text(CONFIG)
    (tmp_path / "prosaic-runtime.yml").write_text(CONFIG.replace("test-model", "other"))
    assert RuntimeConfig.load().profiles["small"].model == "test-model"


@pytest.mark.parametrize("content", ["", "[]", "profiles: [", "!!python/object:builtins.object {}"])
def test_invalid_or_unsafe_yaml_is_rejected(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ValueError, match="configuration"):
        RuntimeConfig.load(path)


def test_toml_is_not_supported(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default_profile = "small"')
    with pytest.raises(ValueError, match="yaml.*yml"):
        RuntimeConfig.load(path)
