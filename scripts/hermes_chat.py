"""Start the Hermes chat for the AP inbox agent.

- runs from the repo folder, so Hermes loads AGENTS.md
- only the inbox MCP tools and the skills toolset are enabled (no terminal, file, web or browser tools)
- the API key comes from ../.env into this process's environment; it is not copied anywhere

Run: python scripts/hermes_chat.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap_agent.config import REPO  # noqa: E402  (importing config loads the .env)

MODEL = os.environ.get("AP_CHAT_MODEL", "claude-sonnet-5-5")


def main():
    hermes = shutil.which("hermes") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin" / "hermes.exe")
    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not found (put it in ../.env)")
    cmd = [hermes, "chat", "--provider", "anthropic", "--model", MODEL, "--toolsets", "mcp-ap_inbox,skills"]
    cmd += sys.argv[1:]  # e.g. -q "question" --oneshot  for a single question
    sys.exit(subprocess.call(cmd, cwd=str(REPO), env=os.environ.copy()))


if __name__ == "__main__":
    main()
