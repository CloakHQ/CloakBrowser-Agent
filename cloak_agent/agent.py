"""The loop: observe → Jev decides → (text model for TYPE_TEXT) → humanized act → repeat."""

import hashlib
import inspect
import time

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserClosed, StalePage
from .model import (
    NeedsInput,
    choose,
    field_context,
    field_text,
    page_view,
    rank_blocks,
    redact,
    resolve,
    resolve_redirects,
    secret_for,
    split_chunks,
    verify_done,
)
from .questions import MAX_STEPS

TOP_BLOCKS = 8


def op_probability(decision):
    """The operation head's p and its best runner-up. A wrong pick showed up here (CLICK 0.43 vs SCROLL 0.35)
    while its target p looked fine (0.71): the target p alone hides that Jev doubted the operation."""
    probs = decision["operation_probabilities"]
    op = decision["operation"]
    rest = sorted(((k, p) for k, p in probs.items() if k != op), key=lambda kv: -kv[1])[:1]
    return {"op": op, "op_p": round(probs[op], 3),
            "op_alt": [(k, round(p, 3)) for k, p in rest if p >= 0.1]}


def op_text(entry):
    """' op=CLICK 0.43 (SCROLL_DOWN 0.35)' for a history entry or op_probability() dict."""
    return f" op={entry['op']} {entry['op_p']}" + "".join(f" ({k} {p})" for k, p in entry["op_alt"])


async def run(session, goal, url=None, page=None, on_step=None, secrets=None):
    """Run one goal in a tab. Returns a result dict; the tab is left open for the caller to close.

    secrets: {name: value} for fields the goal must not spell out (e.g. a password). Jev picks a name per
    field; the value is typed locally and replaced by [secret:name] in everything a model or the caller sees.
    """
    page = page or await session.new_tab(url)
    started = time.perf_counter()
    ms = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
    timing = {"jev_ms": 0, "text_ms": 0}
    history, decisions, text_calls, pending_text, stale = [], 0, 0, None, []
    status, detail, omitted, done_check = "blocked", None, 0, None
    try:
        state = await session.observe(page)
        while True:
            # Elements past snapshot.js's 250 cap are never offered. Measured before deciding on relevance sorting.
            omitted = max(omitted, state.get("omitted_actions", 0))
            if decisions >= MAX_STEPS * 2 or len(history) >= MAX_STEPS:
                status, detail = "budget", f"{len(history)} actions / {decisions} decisions"
                break
            # history too: url/led_to can carry a secret (a login redirecting to ?user=<email>)
            decision = await choose(redact(state, secrets), goal, redact(history, secrets),
                                    secrets=list(secrets or []))
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
                if operation == "DONE":
                    # A DONE pick isn't proof (idealo: DONE 0.61 on a list of the wrong phones). Ask the page
                    # straight and negated; on disagreement the caller checks the page itself.
                    try:
                        done_check = await verify_done(redact(state, secrets), goal, redact(history, secrets))
                    except (RuntimeError, ValueError, KeyError, TypeError) as e:
                        # The task's actions already ran: report them, don't turn a finished task into an error.
                        status, detail = "done_unverified", f"DONE check failed: {type(e).__name__}: {e}"
                        break
                    decisions += 1
                    timing["jev_ms"] += done_check["latency_ms"]
                    if not done_check["verified"]:
                        status = "done_unverified"
                        detail = (f"DONE check disagreed: achieved p={done_check['achieved']}, "
                                  f"not achieved p={done_check['not_achieved']}")
                break
            # Code-level loop brake: the same action on the same page (url + visible text) a 3rd time. Catches
            # cycles that change the page each step (A→B→C→A), which the no-change brake below cannot see.
            # Visible text keeps "Next month" / "Load more" out: their text changes with every click.
            # Scrolls also key on the position: a page whose text doesn't change while scrolling still progresses.
            seen = (state["url"], action["kind"], action["label"],
                    hashlib.sha1(state["text"].encode()).hexdigest()[:12],
                    (state.get("scroll") or {}).get("y") if action["kind"] == "scroll" else None)
            if action["kind"] != "wait" and sum(h.get("seen") == seen for h in history) >= 2:
                status, detail = "blocked", f"Same action {action['label'][:60]!r} on the same page a 3rd time."
                break
            text = shown = None
            try:
                secret = None
                if action["kind"] == "fill" and secrets:
                    secret, latency = await secret_for(redact(action, secrets), list(secrets), goal)
                    decisions += 1
                    timing["jev_ms"] += latency
                if secret:  # typed locally; history and every later model input only see the placeholder
                    text, shown = secrets[secret], f"[secret:{secret}]"
                elif action["kind"] == "fill" and action.get("secret"):
                    raise NeedsInput(action["label"])  # never let the text model invent a password
                elif action["kind"] == "fill":
                    context = field_context(goal, action, redact(state, secrets), history)
                    if pending_text and pending_text[0] == context:
                        text = pending_text[1]
                    else:
                        text, info = await field_text(context)
                        text_calls += 1
                        timing["text_ms"] += info["latency_ms"]
                        pending_text = (context, text)
                    shown = text
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
                "text": shown,
                "probability": decision["probability"],
                "alternatives": decision["alternatives"],
                **op_probability(decision),
                "confidence": decision["confidence"],
                "jev_ms": decision["latency_ms"],
                "at_ms": ms(),
                "url": state["url"],  # where the action happened; with led_to, Jev can see navigation circles
                "seen": seen,  # loop-brake key; not sent to Jev (page_view picks its own keys)
            })
            new_state = await session.observe(page, after=action)
            history[-1]["page_changed"] = new_state["marker"] != state["marker"]
            history[-1]["led_to"] = new_state["url"]
            state = new_state
            if on_step and inspect.isawaitable(reported := on_step(redact(history[-1], secrets))):
                await reported
            last = history[-3:]
            if len(last) == 3 and all(not h["page_changed"] and h["kind"] != "wait" for h in last):
                status, detail = "blocked", "Three actions in a row did not change the page."
                break
        elapsed = ms()
        chunks = redact(split_chunks(await session.settled_markdown(page)), secrets)  # ranked by Jev below
        scores = await rank_blocks(goal, chunks)
        title = await page.title()
    except (BrowserClosed, PlaywrightError, StalePage) as e:
        # The user (or something else) closed the browser or the tab mid-task: report it, keep the trace.
        if not session.is_gone(page):
            raise
        what = "browser was closed" if session.closed else "tab was closed or crashed"
        status, detail = "error", f"the {what} during the task ({type(e).__name__})"
        elapsed, chunks, scores, title = ms(), [], [], ""
    ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
    keep = [i for i in ranked if scores[i] >= 0.5][:TOP_BLOCKS] or ranked[:3]
    return redact({
        "status": status,
        "detail": detail,
        "url": page.url,
        "title": title,
        "elapsed_ms": elapsed,
        "actions": len(history),
        "jev_calls": decisions,
        "text_calls": text_calls,
        "timing": {**timing, "browser_ms": elapsed - timing["jev_ms"] - timing["text_ms"]},
        "stale": stale,
        "omitted_max": omitted,
        "done_check": done_check,
        "trace": history,
        "markdown": await resolve_redirects("\n\n".join(chunks[i] for i in sorted(keep))),
        "block_scores": [round(scores[i], 3) for i in sorted(keep)],
    }, secrets)  # stale diffs, url, title can echo a secret too


async def step(session, page, *, state=None, op=None, target=None, instruction=None, text=None, secrets=None):
    """One action outside the loop, for a caller taking over a stuck task.

    Target mode (`op` + `target` from a snapshot): acts on `state`, the page the caller saw. `text` is typed
    as given, without a model call (the field's value then shows in later views, like any field's).
    Instruction mode: observes, asks Jev once; a field without `text` gets its value from the text model.
    A page that changed since `state` raises StalePage. Nothing is retried: the caller looks again.
    secrets: values a model must not see (the tab's browse secrets); redacted from instruction-mode requests.
    """
    if (op is None) == (instruction is None):
        raise ValueError("give either op (with target) or instruction")
    decision = None
    if op:
        if state is None:
            raise ValueError("no snapshot of this tab yet; call snapshot first")
        _, targets, controls = page_view(state, [])
        action = resolve(targets, controls, op.upper(), target)
        if action["kind"] == "fill" and text is None:
            raise ValueError("TYPE_TEXT needs text")
    else:
        state = await session.observe(page)
        decision = await choose(redact(state, secrets), instruction, [])
        action = decision["action"]
        if decision["operation"] in {"DONE", "BLOCKED"}:
            return {"executed": False, "action": action, "decision": decision, "text": None,
                    "state": state, "page_changed": False}
        if action["kind"] == "fill" and text is None:
            if action.get("secret"):
                raise NeedsInput(action["label"])  # never let the text model invent a password
            text, _ = await field_text(field_context(instruction, action, redact(state, secrets), []))  # NeedsInput propagates
    await session.act(page, state, action, text)
    new_state = await session.observe(page, after=action)
    return {"executed": True, "action": action, "decision": decision, "text": text,
            "state": new_state, "page_changed": new_state["marker"] != state["marker"]}
