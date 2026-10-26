"""Claude Code and direct Anthropic agents, without SDK dependencies."""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol


class Agent(Protocol):
    def ask(self, prompt: str) -> str: ...


def safe_message(message: str) -> str:
    for key, value in os.environ.items():
        if value and ("KEY" in key or "TOKEN" in key or "SECRET" in key):
            message = message.replace(value, "[redacted]")
    return message


class ClaudeCLI:
    def __init__(self, model: str = "") -> None:
        self.model = model or "haiku"

    def binary(self) -> str:
        binary = shutil.which("claude")
        if binary:
            return binary
        for path in (Path.home() / ".claude/local/claude", Path.home() / ".local/bin/claude",
                     Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
            if path.is_file():
                return str(path)
        raise RuntimeError("Claude Code not found. Install claude or configure agent: anthropic-api.")

    def ask(self, prompt: str) -> str:
        argv = [self.binary(), "-p", prompt, "--output-format", "json", "--model", self.model,
                "--tools", "", "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        # Nested Claude Code launches otherwise reuse the parent's auth and fail with 401.
        excluded = {"CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "AI_AGENT", "CLAUDE_EFFORT"}
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("CLAUDE_CODE_") and key not in excluded}
        result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=240)
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            detail = safe_message(result.stderr or result.stdout)[:300]
            raise RuntimeError(f"Claude Code returned invalid JSON: {detail}") from error
        if not isinstance(data, dict):
            raise RuntimeError("Claude Code returned a non-object JSON response.")
        if result.returncode or data.get("is_error") or data.get("subtype") != "success":
            detail = safe_message(str(data.get("result") or result.stderr))[:300]
            raise RuntimeError(f"Claude Code failed: {detail}")
        if not isinstance(data.get("result"), str):
            raise RuntimeError("Claude Code returned no text result.")
        return data["result"]


class AnthropicAPI:
    def __init__(self, model: str = "") -> None:
        self.model = model or "claude-haiku-5-5"

    def ask(self, prompt: str) -> str:
        key = os.environ.get("SFX_LAB_ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Set SFX_LAB_ANTHROPIC_KEY or ANTHROPIC_API_KEY (environment or <lab>/.env).")
        body = {"model": self.model, "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}]}
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            detail = safe_message(error.read().decode("utf-8", errors="replace"))[:300]
            raise RuntimeError(f"Anthropic {error.code}: {detail}") from error
        return "".join(block["text"] for block in data["content"] if block.get("type") == "text")


def load_agent(name: str, model: str = "") -> Agent:
    classes = {"claude-cli": ClaudeCLI, "anthropic-api": AnthropicAPI}
    if name not in classes:
        raise ValueError(f"Unknown agent: {name}")
    return classes[name](model)
