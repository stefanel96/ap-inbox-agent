"""Merge the AP inbox agent settings into Hermes and install the persona.

- backs up <HERMES_HOME>/config.yaml and SOUL.md first (timestamped .bak files)
- adds the ap_inbox MCP server, the external skills folder, write approval, and the Anthropic model
- copies hermes/SOUL.md to <HERMES_HOME>/SOUL.md
The API key is not written anywhere; Hermes gets it from the environment (see scripts/hermes_chat.py).

Run: python scripts/setup_hermes.py
"""
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
if os.environ.get("HERMES_HOME"):
    HOME = Path(os.environ["HERMES_HOME"])
elif os.name == "nt":
    HOME = Path(os.environ["LOCALAPPDATA"]) / "hermes"      # Windows installer default
else:
    HOME = Path.home() / ".hermes"                          # macOS / Linux default
MODEL = os.environ.get("AP_CHAT_MODEL", "claude-sonnet-5-5")


def main():
    if not HOME.exists():
        sys.exit(f"Hermes home not found at {HOME}. Install Hermes first.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cfg_path = HOME / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        shutil.copy2(cfg_path, HOME / f"config.yaml.bak-{stamp}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    repo = REPO.as_posix()
    cfg.setdefault("model", {})
    cfg["model"]["provider"] = "anthropic"
    cfg["model"]["default"] = MODEL
    # The default config may carry another provider's endpoint (e.g. OpenRouter). With the Anthropic provider it
    # must go: otherwise the Anthropic key would be sent to that third-party URL.
    cfg["model"].pop("base_url", None)
    cfg.setdefault("mcp_servers", {})["ap_inbox"] = {
        "command": Path(sys.executable).as_posix(),  # the Python running this script (the project venv), any OS
        "args": ["-m", "ap_agent.mcp_server"],
        "cwd": repo,
        "timeout": 120,
        "connect_timeout": 60,
    }
    skills = cfg.setdefault("skills", {})
    dirs = skills.setdefault("external_dirs", []) or []
    if f"{repo}/hermes/skills" not in dirs:
        dirs.append(f"{repo}/hermes/skills")
    skills["external_dirs"] = dirs
    skills["write_approval"] = True
    cfg.setdefault("memory", {})["write_approval"] = True
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    soul = HOME / "SOUL.md"
    if soul.exists():
        shutil.copy2(soul, HOME / f"SOUL.md.bak-{stamp}")
    shutil.copy2(REPO / "hermes" / "SOUL.md", soul)
    print(f"updated {cfg_path} (backup: config.yaml.bak-{stamp}) and {soul}")


if __name__ == "__main__":
    main()
