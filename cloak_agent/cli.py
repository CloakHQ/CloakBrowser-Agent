"""cloak-agent browser  → start a headed CloakBrowser with a CDP port and keep it running.
cloak-agent run      → run one goal (connects with --cdp, otherwise launches its own browser)."""

import argparse
import asyncio
import json

from .agent import run
from .browser import DEFAULT_PROFILE, Session


def print_step(step):
    text = f" ← {step['text']!r}" if step["text"] else ""
    print(f"[{step['at_ms']:>6} ms] {step['kind']:6} {step['action'][:70]}{text}  (p={step['probability']})", flush=True)


async def serve_browser(args):
    session = await Session.launch(profile=args.profile, headless=args.headless, cdp_port=args.port)
    print(f"CloakBrowser running. Connect with: --cdp http://127.0.0.1:{args.port}   (Ctrl-C to close)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await session.close()


async def run_goal(args):
    humanize = not args.no_humanize
    if args.cdp:
        session = await Session.connect(args.cdp, humanize=humanize)
    else:
        session = await Session.launch(profile=args.profile, headless=args.headless, humanize=humanize)
    try:
        page = await session.new_tab(args.url)
        result = await run(session, args.goal, page=page, on_step=print_step)
        print(json.dumps({k: v for k, v in result.items() if k != "trace"}, indent=2, ensure_ascii=False))
        if args.keep_open and not args.cdp:
            print("Browser kept open (Ctrl-C to close).", flush=True)
            await asyncio.Event().wait()
        # connect mode: the tab stays open in the running browser for inspection.
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(prog="cloak-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("browser")
    b.add_argument("--port", type=int, default=9222)
    r = sub.add_parser("run")
    r.add_argument("--goal", required=True)
    r.add_argument("--url")
    r.add_argument("--cdp", help="e.g. http://127.0.0.1:9222 (from `cloak-agent browser`)")
    r.add_argument("--keep-open", action="store_true")
    r.add_argument("--no-humanize", action="store_true", help="instant clicks/typing: faster, less human-looking")
    for p in (b, r):
        p.add_argument("--profile", default=str(DEFAULT_PROFILE))
        p.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(serve_browser(args) if args.command == "browser" else run_goal(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
