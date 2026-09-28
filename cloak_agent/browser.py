"""CloakBrowser session: launch or connect over CDP, observe in the isolated world, act through humanize.

Uses cloakbrowser internals (pinned version): page._stealth_world, page._human_raw_mouse,
page._human_cfg, human._SELECT_ALL, human.scroll_async._async_smooth_wheel.
"""

import asyncio
import json
import random
from pathlib import Path

from cloakbrowser import launch_persistent_context_async
from cloakbrowser.human import _SELECT_ALL, _AsyncIsolatedWorld, patch_context_async
from cloakbrowser.human.config import resolve_config
from cloakbrowser.human.mouse import click_target
from cloakbrowser.human.mouse_async import async_human_click
from cloakbrowser.human.scroll_async import _async_smooth_wheel
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
SNAPSHOT_JS = (HERE / "snapshot.js").read_text()
MARKDOWN_JS = (HERE / "markdown.js").read_text()
DEFAULT_PROFILE = Path.home() / ".cloakbrowser-agent" / "profile"


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Session:
    """launch mode: we own the browser. connect mode: attach to a running one and leave it running."""

    def __init__(self, context, owner=None, humanize=True):
        self.context = context
        self._owner = owner  # (playwright, browser) in connect mode
        # humanize=False: instant clicks and typing (still real input events). Faster, less human-looking.
        self.humanize = humanize

    @classmethod
    async def launch(cls, profile=DEFAULT_PROFILE, headless=False, cdp_port=None, humanize=True, **kwargs):
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        context = await launch_persistent_context_async(
            str(profile), headless=headless, humanize=humanize, args=args, **kwargs
        )
        return cls(context, humanize=humanize)

    @classmethod
    async def connect(cls, cdp_url, humanize=True, preset="default"):
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        if humanize:
            patch_context_async(context, resolve_config(preset))
        return cls(context, owner=(pw, browser), humanize=humanize)

    async def new_tab(self, url=None):
        page = await self.context.new_page()
        if getattr(page, "_stealth_world", None) is None:
            # Not humanize-patched: still read the DOM only from an isolated world, never the main world.
            world = page._stealth_world = _AsyncIsolatedWorld(page)
            page.on("framenavigated", lambda frame: world.invalidate() if frame == page.main_frame else None)
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def close(self):
        """launch mode closes the browser; connect mode only drops our connection."""
        if self._owner:
            pw, _ = self._owner
            await pw.stop()  # disconnects; the remote browser keeps running
        else:
            await self.context.close()

    @staticmethod
    async def _eval(page, expression):
        return await page._stealth_world.evaluate(expression)

    async def observe(self, page, after=None):
        if after and after["kind"] == "fill" and after.get("role") == "combobox":
            # Let autocomplete suggestions arrive before Jev chooses from an incomplete popup.
            for _ in range(8):
                await asyncio.sleep(0.025)
                if await self._eval(page, _OPTIONS_VISIBLE):
                    break
        elif after and after["kind"] != "wait":
            await asyncio.sleep(0.05)
        state = previous = None
        for attempt in range(100):  # up to ~10 s while a navigation settles
            state = await self._eval(page, SNAPSHOT_JS) or state
            # Pages keep mutating after load (late JS panels, lazy widgets), which invalidates decisions.
            # Wait until two snapshots 100 ms apart agree, capped at ~3 s.
            # A blank document (no text, no elements) is a page still booting, not a settled one.
            blank = state and not state["text"] and not any("node" in a for a in state["actions"])
            quiet = previous is not None and state and not blank and state["marker"] == previous["marker"]
            if state and ((state["ready"] == "complete" and quiet) or attempt >= 30):
                return state
            previous = state
            await asyncio.sleep(0.1)
        if state:
            return state
        raise StalePage("Page did not settle")

    async def fresh(self, page, state, action=None):
        self.last_diff = ""
        if action is not None and action["kind"] in {"click", "select"}:
            current = await self._eval(
                page,
                f"(() => {{ const c=window.__ca; return c ? [JSON.stringify(c.pageKey()),"
                f"JSON.stringify(c.guard(c.nodes.get({int(action['node'])})))] : null; }})()",
            )
            return current == [state["page_key"], state["guards"].get(str(action["node"]))]
        current = await self._eval(page, SNAPSHOT_JS)
        same = bool(current) and current["marker"] == state["marker"]
        if current and not same:
            self.last_diff = _marker_diff(state["marker"], current["marker"])  # DEBUG: why was it stale
        return same

    async def act(self, page, state, action, text=None):
        if not await self.fresh(page, state, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind, cfg = action["kind"], getattr(page, "_human_cfg", None)
        if kind == "wait":
            await asyncio.sleep(0.1)
            return
        if kind == "scroll":
            if self.humanize:
                await _async_smooth_wheel(page._human_raw_mouse, action["delta"], cfg)
            else:
                await page.mouse.wheel(0, action["delta"])
            return
        node = int(action["node"])
        if kind == "select":
            ok = await self._eval(page, f"window.__ca?.select({node}, {json.dumps(action['value'])})")
            if not ok:
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            return
        box = await self._eval(page, f"window.__ca?.box({node}, {json.dumps(kind)})")
        if not box:
            raise StalePage("Target changed or is covered. Observe again.")
        if not self.humanize:
            # box() already hit-tested the center. Playwright's plain click/type: real input events, no delays.
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            if kind == "fill":
                await page.keyboard.press(_SELECT_ALL)
                await page.keyboard.press("Backspace")
                await page.keyboard.type(text)
            return
        # Humanized aim points can land on overlays inside the box (e.g. a search icon over an input's
        # left edge). Pick one that actually hits the node before spending a mouse move on it.
        for _ in range(5):
            point = click_target(box, box["input"], cfg)
            if await self._eval(page, f"window.__ca?.hit({node}, {point.x}, {point.y})"):
                break
        else:
            raise StalePage("No aim point inside the target hits it; it is covered.")
        await page.mouse.move(point.x, point.y)  # humanized bezier path
        # The move takes hundreds of ms; the page may have shifted. Re-hit-test before pressing.
        if not await self._eval(page, f"window.__ca?.hit({node}, {point.x}, {point.y})"):
            raise StalePage("Target moved or became covered during the approach.")
        await async_human_click(page._human_raw_mouse, box["input"], cfg)
        if kind == "fill":
            await asyncio.sleep(random.uniform(0.1, 0.25))
            await page.keyboard.press(_SELECT_ALL)
            await asyncio.sleep(random.uniform(0.03, 0.08))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.keyboard.type(text)  # humanized per-key typing

    async def markdown(self, page):
        return await self._eval(page, MARKDOWN_JS) or ""


MARKER_PARTS = ["timeOrigin", "url", "scrollX", "scrollY", "innerWidth", "innerHeight", "title", "text", "actions",
                "form_values"]


def _marker_diff(old, new):
    """DEBUG: name the marker parts that changed, with a short sample of the change."""
    old, new = json.loads(old), json.loads(new)
    out = []
    for name, a, b in zip(MARKER_PARTS, old, new):
        if a == b:
            continue
        if name == "text":
            added = set(b.split("\n")) - set(a.split("\n"))
            removed = set(a.split("\n")) - set(b.split("\n"))
            out.append(f"text +{sorted(added)[:3]} -{sorted(removed)[:3]}")
        elif name == "actions":
            ka = {(x.get("label"), x.get("kind"), x.get("value")) for x in a}
            kb = {(x.get("label"), x.get("kind"), x.get("value")) for x in b}
            out.append(f"actions +{sorted(map(str, kb - ka))[:3]} -{sorted(map(str, ka - kb))[:3]}")
        else:
            out.append(f"{name} {str(a)[:60]} → {str(b)[:60]}")
    return "; ".join(out)


_OPTIONS_VISIBLE = """[...document.querySelectorAll('[role="option"]')].some(e => {
  const r = e.getBoundingClientRect();
  return r.width && r.height && r.bottom > 0 && r.top < innerHeight &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
})"""
