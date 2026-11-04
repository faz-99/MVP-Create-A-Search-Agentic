"""Command line: python -m create_search [--provider openai|claude] [--apps SF,AOE] "request"

Prints progress events, then the created search as JSON. Uses LEXIS_SSO_COOKIE from .env.
"""

import argparse
import asyncio
import json

from create_search.run import create_search


def _show(ev: dict) -> None:
    kind = ev["kind"]
    if kind in ("done", "metrics", "step", "ping"):
        return
    message = ev["message"]
    if kind == "trace":
        message = message[:300]
    print(f"[{kind}] {message}")


async def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m create_search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--provider", default="openai", choices=["openai", "claude"])
    parser.add_argument("--apps", default="SF,AOE", help="comma-separated, starting app first")
    parser.add_argument("--model")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()

    done = None
    async for ev in create_search(
        " ".join(args.query),
        provider=args.provider,
        apps=args.apps.split(","),
        model=args.model,
        effort=args.effort,
    ):
        _show(ev)
        if ev["kind"] == "metrics":
            m = ev["message"]
            print(
                f"[metrics] {m['total_ms']} ms total, {m['model_calls']} model calls, "
                f"{m['tool_calls']} tool calls, {m['input_tokens']} in "
                f"({m['cached_tokens']} cached) / {m['output_tokens']} out"
            )
        if ev["kind"] == "done":
            done = ev["message"]
    if done is None:
        return 1
    print(json.dumps(done, indent=2, ensure_ascii=False))
    return 0 if done.get("runnable") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
