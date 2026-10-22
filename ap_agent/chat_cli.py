"""Fallback chat in the terminal, without Hermes: same tools, same instructions (SOUL.md + AGENTS.md + SKILL.md).

Run: python -m ap_agent.chat_cli
"""
import inspect
import json
import os

from .config import REPO
from .llm import client, cost_usd
from .tools import TOOLS

CHAT_MODEL = os.environ.get("AP_CHAT_MODEL", "claude-sonnet-5-5")
_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema(fn):
    props, required = {}, []
    for name, p in inspect.signature(fn).parameters.items():
        props[name] = {"type": _TYPES.get(p.annotation, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {"name": fn.__name__, "description": inspect.getdoc(fn),
            "input_schema": {"type": "object", "properties": props, "required": required}}


def _instructions():
    parts = []
    for rel in ("hermes/SOUL.md", "AGENTS.md", "hermes/skills/ap-inbox-analyst/SKILL.md"):
        p = REPO / rel
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def main():
    tools = [_schema(f) for f in TOOLS]
    by_name = {f.__name__: f for f in TOOLS}
    system = _instructions()
    messages, spent = [], 0.0
    print(f"AP inbox agent ({CHAT_MODEL}). Ask a question, or 'quit'.")
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("quit", "exit"):
            break
        if not q:
            continue
        messages.append({"role": "user", "content": q})
        while True:
            resp = client().messages.create(model=CHAT_MODEL, max_tokens=8000, system=system, tools=tools,
                                            messages=messages, output_config={"effort": "medium"})
            spent += cost_usd(CHAT_MODEL, resp.usage)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    print(f"  [tool] {block.name}({json.dumps(block.input)[:120]})")
                    try:
                        out = json.dumps(by_name[block.name](**block.input), default=str)[:60000]
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                    except Exception as e:
                        results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                        "content": f"{type(e).__name__}: {e}"})
            messages.append({"role": "user", "content": results})
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        print(f"\nagent> {text}\n\n  (session cost so far: ${spent:.3f})")


if __name__ == "__main__":
    main()
