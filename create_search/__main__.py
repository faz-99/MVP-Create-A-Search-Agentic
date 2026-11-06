"""CLI: python -m create_search [--provider openai|claude] [--apps SF,AOE] "request" (uses LEXIS_SSO_COOKIE)."""

import argparse
import asyncio
import json

from create_search.run import create_search


async def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m create_search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--provider", default="openai", choices=["openai", "claude"])
    parser.add_argument("--apps", default="SF,AOE", help="comma-separated, starting app first")
    parser.add_argument("--model")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()

    done = None
    async for ev in create_search(" ".join(args.query), provider=args.provider, apps=args.apps.split(","), model=args.model, effort=args.effort):
        kind, msg = ev["kind"], ev["message"]
        if kind == "metrics":
            print(f"[metrics] {msg['total_ms']} ms total, {msg['model_calls']} model calls, {msg['tool_calls']} tool calls, "
                  f"{msg['input_tokens']} in ({msg['cached_tokens']} cached) / {msg['output_tokens']} out")
        elif kind == "done":
            done = msg
        elif kind not in ("step", "ping"):
            print(f"[{kind}] {msg[:300] if kind == 'trace' else msg}")
    if done is None:
        return 1
    print(json.dumps(done, indent=2, ensure_ascii=False))
    return 0 if done.get("runnable") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
