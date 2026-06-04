import json

from src.config.settings import Config


def write_config(path, *, host="127.0.0.1", port=7000):
    path.write_text(
        json.dumps(
            {
                "openai": {
                    "api_key": "test-openai-key",
                    "base_url": "https://api.openai.com/v1",
                },
                "server": {
                    "host": host,
                    "port": port,
                },
                "api_key": "test-proxy-key",
            }
        ),
        encoding="utf-8",
    )


def test_server_config_uses_json_when_no_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("SERVER_HOST", raising=False)
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    config_path = tmp_path / "settings.json"
    write_config(config_path)

    config = Config.from_file_sync(str(config_path))

    assert config.get_server_config() == ("127.0.0.1", 7000)


def test_server_port_env_overrides_json_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_PORT", "8100")
    monkeypatch.setenv("PORT", "8200")
    config_path = tmp_path / "settings.json"
    write_config(config_path)

    config = Config.from_file_sync(str(config_path))

    assert config.server.port == 8100


def test_platform_port_env_is_fallback_override(tmp_path, monkeypatch):
    monkeypatch.delenv("SERVER_PORT", raising=False)
    monkeypatch.setenv("PORT", "8200")
    config_path = tmp_path / "settings.json"
    write_config(config_path)

    config = Config.from_file_sync(str(config_path))

    assert config.server.port == 8200


def test_server_host_env_overrides_json_host(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    config_path = tmp_path / "settings.json"
    write_config(config_path, host="127.0.0.1")

    config = Config.from_file_sync(str(config_path))

    assert config.server.host == "0.0.0.0"
