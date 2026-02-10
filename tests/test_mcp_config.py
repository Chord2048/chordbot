"""Unit tests for MCP config loading."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from chordcode.mcp.config import (
    MCPServerConfig,
    load_mcp_configs,
    _parse_server_entry,
    _load_file,
)


class TestParseServerEntry:
    def test_local_server_with_command(self):
        raw = {"command": "npx", "args": ["-y", "mcp-server"]}
        cfg = _parse_server_entry("test", raw, source="test.json")
        assert cfg is not None
        assert cfg.name == "test"
        assert cfg.type == "local"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "mcp-server"]
        assert cfg.transport == "stdio"

    def test_remote_server_with_url(self):
        raw = {"url": "https://example.com/mcp"}
        cfg = _parse_server_entry("remote", raw, source="test.json")
        assert cfg is not None
        assert cfg.name == "remote"
        assert cfg.type == "remote"
        assert cfg.url == "https://example.com/mcp"
        assert cfg.transport == "streamable-http"

    def test_remote_server_explicit_sse(self):
        raw = {"url": "https://example.com/sse", "transport": "sse"}
        cfg = _parse_server_entry("sse", raw, source="test.json")
        assert cfg is not None
        assert cfg.transport == "sse"

    def test_remote_server_with_headers(self):
        raw = {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        cfg = _parse_server_entry("auth", raw, source="test.json")
        assert cfg is not None
        assert cfg.headers == {"Authorization": "Bearer tok"}

    def test_disabled_server(self):
        raw = {"command": "npx", "enabled": False}
        cfg = _parse_server_entry("off", raw, source="test.json")
        assert cfg is not None
        assert cfg.enabled is False

    def test_missing_command_and_url(self):
        raw = {"timeout": 10}
        cfg = _parse_server_entry("bad", raw, source="test.json")
        assert cfg is None

    def test_non_dict_entry(self):
        cfg = _parse_server_entry("bad", "not a dict", source="test.json")
        assert cfg is None

    def test_custom_timeout(self):
        raw = {"command": "node", "timeout": 60}
        cfg = _parse_server_entry("slow", raw, source="test.json")
        assert cfg is not None
        assert cfg.timeout == 60

    def test_env_vars(self):
        raw = {"command": "node", "env": {"FOO": "bar"}}
        cfg = _parse_server_entry("env", raw, source="test.json")
        assert cfg is not None
        assert cfg.env == {"FOO": "bar"}


class TestLoadFile:
    def test_missing_file(self):
        assert _load_file("/nonexistent/path/mcp.json") is None

    def test_valid_json(self, tmp_path):
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps({"mcpServers": {}}))
        result = _load_file(str(p))
        assert result == {"mcpServers": {}}

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "mcp.json"
        p.write_text("{bad json")
        assert _load_file(str(p)) is None

    def test_non_dict_root(self, tmp_path):
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps([1, 2, 3]))
        assert _load_file(str(p)) is None


class TestLoadMcpConfigs:
    @pytest.fixture(autouse=True)
    def _isolate_global_paths(self, monkeypatch):
        """Prevent real global configs from interfering with tests."""
        import chordcode.mcp.config as config_mod
        monkeypatch.setattr(config_mod, "GLOBAL_SCAN_PATHS", ())

    def test_no_config_files(self, tmp_path):
        configs = load_mcp_configs(str(tmp_path / "nonexistent"))
        assert configs == {}

    def test_single_local_server(self, tmp_path):
        # Create a project-level config
        mcp_dir = tmp_path / ".chordcode"
        mcp_dir.mkdir()
        config_file = mcp_dir / "mcp.json"
        config_file.write_text(json.dumps({
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                }
            }
        }))

        configs = load_mcp_configs(str(tmp_path))
        assert "filesystem" in configs
        cfg = configs["filesystem"]
        assert cfg.type == "local"
        assert cfg.command == "npx"
        assert cfg.transport == "stdio"

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        # Create global config
        global_dir = tmp_path / "global" / ".chordcode"
        global_dir.mkdir(parents=True)
        global_config = global_dir / "mcp.json"
        global_config.write_text(json.dumps({
            "mcpServers": {
                "myserver": {"command": "old-cmd", "args": ["--old"]}
            }
        }))

        # Create project config
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        proj_mcp = project_dir / ".chordcode"
        proj_mcp.mkdir()
        proj_config = proj_mcp / "mcp.json"
        proj_config.write_text(json.dumps({
            "mcpServers": {
                "myserver": {"command": "new-cmd", "args": ["--new"]}
            }
        }))

        # Monkeypatch the scan paths to use our temp dirs
        import chordcode.mcp.config as config_mod
        original_global = config_mod.GLOBAL_SCAN_PATHS
        monkeypatch.setattr(
            config_mod, "GLOBAL_SCAN_PATHS",
            (str(global_config),),
        )

        configs = load_mcp_configs(str(project_dir))
        assert "myserver" in configs
        # Project should override global
        assert configs["myserver"].command == "new-cmd"
        assert configs["myserver"].args == ["--new"]

    def test_multiple_servers(self, tmp_path):
        mcp_dir = tmp_path / ".chordcode"
        mcp_dir.mkdir()
        config_file = mcp_dir / "mcp.json"
        config_file.write_text(json.dumps({
            "mcpServers": {
                "local": {"command": "node", "args": ["server.js"]},
                "remote": {"url": "https://api.example.com/mcp"},
                "disabled": {"command": "skip", "enabled": False},
            }
        }))

        configs = load_mcp_configs(str(tmp_path))
        assert len(configs) == 3
        assert configs["local"].type == "local"
        assert configs["remote"].type == "remote"
        assert configs["disabled"].enabled is False
