"""The loop: observe → Jev decides → (text model for TYPE_TEXT) → humanized act → repeat."""

import inspect
import time

from .browser import StalePage
from .model import NeedsInput, choose, field_context, field_text, rank_blocks, resolve_redirects, split_chunks
from .questions import MAX_STEPS

TOP_BLOCKS = 8


async def run(session, goal, url=None, page=None, on_step=None):
    """Run one goal in a tab. Returns a result dict; the tab is left open for the caller to close."""
    page = page or await session.new_tab(url)
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    timing = {"jev_ms": 0, "text_ms": 0}
    history, decisions, text_calls, pending_text, stale = [], 0, 0, None, []
    status, detail = "blocked", None
    state = await session.observe(page)
    while True:
        if decisions >= MAX_STEPS * 2 or len(history) >= MAX_STEPS:
            status, detail = "budget", f"{len(history)} actions / {decisions} decisions"
            break
        decision = await choose(state, goal, history)
        decisions += 1
        timing["jev_ms"] += decision["latency_ms"]
        action, operation = decision["action"], decision["operation"]
        if operation in {"DONE", "BLOCKED"}:
            if not await session.fresh(page, state):
                stale.append(f"{ms()}ms {operation}: page changed before completion"
                             f" [{getattr(session, 'last_diff', '')}]")  # what changed
                state = await session.observe(page)
                continue
            status = operation.lower()
            break
        text = None
        try:
            if action["kind"] == "fill":
                context = field_context(goal, action, state, history)
                if pending_text and pending_text[0] == context:
                    text = pending_text[1]
                else:
                    text, info = await field_text(context)
                    text_calls += 1
                    timing["text_ms"] += info["latency_ms"]
                    pending_text = (context, text)
            await session.act(page, state, action, text)
        except NeedsInput as e:
            status, detail = "needs_input", f"No value in the goal for field: {e}"
            break
        except StalePage as e:
            stale.append(f"{ms()}ms {action['kind']} {action['label'][:40]!r}: {e}"
                         f" [{getattr(session, 'last_diff', '')}]")  # what changed
            state = await session.observe(page)
            continue
        pending_text = None
        history.append({
            "step": len(history) + 1,
            "action": action["label"],
            "kind": action["kind"],
            "role": action.get("role"),
            "text": text,
            "probability": decision["probability"],
            "alternatives": decision["alternatives"],
            "confidence": decision["confidence"],
            "jev_ms": decision["latency_ms"],
            "at_ms": ms(),
        })
        new_state = await session.observe(page, after=action)
        history[-1]["page_changed"] = new_state["marker"] != state["marker"]
        state = new_state
        if on_step and inspect.isawaitable(reported := on_step(history[-1])):
            await reported
        last = history[-3:]
        if len(last) == 3 and all(not h["page_changed"] and h["kind"] != "wait" for h in last):
            status, detail = "blocked", "Three actions in a row did not change the page."
            break
    elapsed = ms()
    chunks = split_chunks(await session.markdown(page))
    scores = await rank_blocks(goal, chunks)
    ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
    keep = [i for i in ranked if scores[i] >= 0.5][:TOP_BLOCKS] or ranked[:3]
    return {
        "status": status,
        "detail": detail,
        "url": page.url,
        "title": await page.title(),
        "elapsed_ms": elapsed,
        "actions": len(history),
        "jev_calls": decisions,
        "text_calls": text_calls,
        "timing": {**timing, "browser_ms": elapsed - timing["jev_ms"] - timing["text_ms"]},
        "stale": stale,
        "trace": history,
        "markdown": await resolve_redirects("\n\n".join(chunks[i] for i in sorted(keep))),
        "block_scores": [round(scores[i], 3) for i in sorted(keep)],
    }
