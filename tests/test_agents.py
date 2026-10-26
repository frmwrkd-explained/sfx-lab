import io
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from sfx_lab.agents import AnthropicAPI, ClaudeCLI, load_agent, safe_message


def test_claude_argv_env_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/fake/claude")
    for key in ("CLAUDE_CODE_ONE", "CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "AI_AGENT", "CLAUDE_EFFORT"):
        monkeypatch.setenv(key, "parent")
    monkeypatch.setenv("KEEP_TEST_ENV", "yes")
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"subtype":"success","result":"answer"}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ClaudeCLI().ask("hello") == "answer"
    assert seen["argv"] == ["/fake/claude", "-p", "hello", "--output-format", "json", "--model", "haiku", "--tools", "", "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
    assert seen["timeout"] == 240 and seen["capture_output"] and seen["text"]
    assert seen["env"]["KEEP_TEST_ENV"] == "yes"
    assert not any(key.startswith("CLAUDE_CODE_") or key in {"CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "AI_AGENT", "CLAUDE_EFFORT"} for key in seen["env"])


@pytest.mark.parametrize(("stdout", "stderr", "code", "message"), [
    ('{"subtype":"error","result":"oops"}', "", 0, "oops"),
    ('{"subtype":"success","is_error":true,"result":"denied"}', "", 0, "denied"),
    ('{"subtype":"success","result":""}', "bad exit", 1, "bad exit"),
    ("not json", "invalid reply", 0, "invalid reply"),
    ('{"result":"missing subtype"}', "", 0, "missing subtype"),
    ("[]", "", 0, "non-object"),
])
def test_claude_errors(monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str,
                       code: int, message: str) -> None:
    monkeypatch.setattr(ClaudeCLI, "binary", lambda self: "/fake/claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, code, stdout, stderr))
    with pytest.raises(RuntimeError, match=message):
        ClaudeCLI().ask("hello")


def test_claude_binary_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda self: self == tmp_path / ".local/bin/claude")
    assert ClaudeCLI().binary() == str(tmp_path / ".local/bin/claude")
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(RuntimeError, match="Claude Code not found"):
        ClaudeCLI().binary()


def test_anthropic_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFX_LAB_ANTHROPIC_KEY", "preferred-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fallback-secret")
    seen: dict[str, Any] = {}

    def urlopen(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data or b""), timeout=timeout, method=request.method)
        return io.BytesIO(b'{"content":[{"type":"text","text":"one"},{"type":"tool_use"},{"type":"text","text":"two"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert AnthropicAPI().ask("hello") == "onetwo"
    assert seen == {"url": "https://api.anthropic.com/v1/messages", "headers": {"X-api-key": "preferred-secret", "Anthropic-version": "2023-06-01", "Content-type": "application/json"}, "body": {"model": "claude-haiku-5-5", "max_tokens": 4000, "messages": [{"role": "user", "content": "hello"}]}, "timeout": 120, "method": "POST"}
    monkeypatch.delenv("SFX_LAB_ANTHROPIC_KEY")
    assert AnthropicAPI("custom").ask("hello") == "onetwo"
    assert seen["headers"]["X-api-key"] == "fallback-secret"
    assert seen["body"]["model"] == "custom"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="Set SFX_LAB_ANTHROPIC_KEY"):
        AnthropicAPI().ask("hello")


def test_loader_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(load_agent("claude-cli"), ClaudeCLI)
    assert isinstance(load_agent("anthropic-api"), AnthropicAPI)
    with pytest.raises(ValueError, match="Unknown agent"):
        load_agent("other")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sensitive-value")
    assert "sensitive-value" not in safe_message("error sensitive-value")
