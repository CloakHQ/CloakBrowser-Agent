import asyncio
import json
import time

import pytest

from cloak_agent import model

ACTIONS = [
    {"id": "e1", "node": 1, "kind": "fill", "role": "combobox", "label": "Search", "value": ""},
    {"id": "e2", "node": 1, "kind": "click", "role": "combobox", "label": "Open Search", "value": ""},
    {"id": "e3", "node": 2, "kind": "click", "role": "checkbox", "label": "Free cancellation", "checked": "false"},
    {"id": "e4", "node": 3, "kind": "select", "role": "combobox", "label": "Sort → Price", "value": "price",
     "current_value": "Relevance"},
    {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
    {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
]


def test_action_space_one_index_per_node_and_per_operation_targets():
    elements, targets, controls = model.action_space(ACTIONS)
    assert [e["index"] for e in elements] == ["1", "2", "3"]
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert set(targets) == {"TYPE_TEXT", "CLICK", "SELECT"}
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["SELECT"]["3:1"]["value"] == "price"
    assert set(controls) == {"SCROLL_DOWN", "WAIT"}


def test_validate_choice_rejects_off_menu_and_bad_distributions():
    ok = {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}
    assert model.validate_choice(ok, {"a": 1, "b": 1}) is ok
    for bad in (
        {**ok, "choice": "c"},
        {**ok, "probabilities": {"a": 0.9}},
        {**ok, "probabilities": {"a": 0.5, "b": 0.2}},
        {**ok, "choice": "b"},  # not the argmax
        {},
    ):
        with pytest.raises(ValueError):
            model.validate_choice(bad, {"a": 1, "b": 1})


def _fake_post(content):
    async def post(url, key, body, headers=None):
        post.body, post.headers = body, headers
        return {"choices": [{"message": {"content": content}}]}
    return post


CTX = {"goal": "search cats", "field": {"label": "Search"}, "page": {}, "recent_actions": []}


def test_field_text_generic_endpoint_headers_and_reasoning(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setenv("TEXT_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "high")
    monkeypatch.setenv("TEXT_MODEL_HEADERS", json.dumps({"x-opencode-session": "s"}))
    post = _fake_post('{"text": "cats"}')
    monkeypatch.setattr(model, "post_json", post)
    value, info = asyncio.run(model.field_text(CTX))
    assert value == "cats" and info["model"] == "deepseek-v4-flash"
    assert post.body["reasoning_effort"] == "high" and post.headers == {"x-opencode-session": "s"}


def test_field_text_null_means_needs_input_and_junk_is_rejected(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k")
    monkeypatch.setattr(model, "post_json", _fake_post('{"text": null}'))
    with pytest.raises(model.NeedsInput):
        asyncio.run(model.field_text(CTX))
    for junk in ('{"text": "a", "extra": 1}', "not json", '{"text": 5}'):
        monkeypatch.setattr(model, "post_json", _fake_post(junk))
        with pytest.raises(ValueError):
            asyncio.run(model.field_text(CTX))


def test_choose_sends_step_urls_in_recent_actions(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def post(url, key, body, headers=None):
        post.body = body
        return {"model": "jev", "answers": {"operation": {
            "choice": "WAIT", "confidence": 1.0, "probabilities": {"WAIT": 1.0, "DONE": 0.0, "BLOCKED": 0.0}}}}
    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "https://a/buy", "title": "T", "text": "", "actions": [
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]}
    history = [{"action": "iPhone", "kind": "click", "text": None, "page_changed": True,
                "url": "https://a/buy", "led_to": "https://a/iphone"}]
    asyncio.run(model.choose(page, "goal", history))
    sent = post.body["state"]["recent_actions"][0]
    assert sent["url"] == "https://a/buy" and sent["led_to"] == "https://a/iphone"


def test_choose_request_is_compact(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def post(url, key, body, headers=None):
        post.body = body
        return {"model": "jev", "answers": {
            "operation": {"choice": "CLICK", "confidence": 1.0, "probabilities": {
                k: float(k == "CLICK") for k in body["questions"]["operation"]["criteria"]}},
            "click_target": {"choice": "2", "confidence": 1.0, "probabilities": {
                k: float(k == "2") for k in body["questions"]["click_target"]["criteria"]}},
            "type_text_target": {"choice": "1", "confidence": 1.0, "probabilities": {"1": 1.0}},
            "select_target": {"choice": "3:1", "confidence": 1.0, "probabilities": {"3:1": 1.0}}}}
    monkeypatch.setattr(model, "post_json", post)
    page = {"url": "u", "title": "t", "text": "", "actions": ACTIONS}
    asyncio.run(model.choose(page, "goal", []))
    body = json.dumps(post.body)
    assert body.count(json.dumps(model.NEXT_ACTION)) == 1  # full rules sent once, in the operation question
    # target questions get their own short element-choice rules (without them: Accept instead of Reject)
    assert "reject/decline" in post.body["questions"]["click_target"]["instructions"]["rules"]
    assert post.body["questions"]["click_target"]["criteria"]["2"] == "[2] Free cancellation"
    # elements as one line each, explained by the legend
    assert post.body["state"]["element_format"] == model.ELEMENT_FORMAT
    assert post.body["state"]["elements"] == [
        '[1] combobox "Search" ops=TYPE_TEXT,CLICK',
        '[2] checkbox "Free cancellation" checked=false',
        '[3] combobox "Sort" value="Relevance" ops=SELECT options: 3:1 Price',
    ]


def test_settled_markdown_waits_for_late_content_and_caps():
    from cloak_agent.browser import Session

    class FakeWorld:
        def __init__(self, frames):
            self.frames = iter(frames)

        async def evaluate(self, _):
            return next(self.frames, self.last)  # after the scripted frames, the page stays as the last one

    class FakePage:
        def __init__(self, frames):
            self._stealth_world = FakeWorld(frames)
            self._stealth_world.last = frames[-1]

        def is_closed(self):
            return False

    class FakeContext:
        def on(self, *_):
            pass

    s = Session(FakeContext())
    # results arrive after two reads, then stay: we must return the final content, not the early one
    page = FakePage(["loading", "loading", "results: 22 flights"])
    out = asyncio.run(s.settled_markdown(page, quiet=0.2, cap=2.0, every=0.05))
    assert out == "results: 22 flights"
    # a page that never stops changing returns at the cap
    endless = FakePage([str(i) for i in range(1000)])
    t = time.perf_counter()
    asyncio.run(s.settled_markdown(endless, quiet=0.5, cap=0.3, every=0.05))
    assert time.perf_counter() - t < 1.0


def test_scroll_gauge():
    assert model.scroll_gauge({"y": 0, "height": 800, "vh": 870}) == "all"
    assert model.scroll_gauge({"y": 0, "height": 2000, "vh": 800}) == "0-40% (more below)"
    assert model.scroll_gauge({"y": 1200, "height": 2000, "vh": 800}) == "60-100% (end)"


def test_legacy_seed_is_handed_to_the_wrapper_once(tmp_path):
    from cloak_agent.browser import LEGACY_SEED_FILE, PROFILE_SEED_FILE, adopt_legacy_seed
    old, new = tmp_path / LEGACY_SEED_FILE, tmp_path / PROFILE_SEED_FILE
    adopt_legacy_seed(tmp_path)
    assert not new.exists()  # no old seed → the wrapper picks one
    old.write_text("24911")
    adopt_legacy_seed(tmp_path)
    assert new.read_text() == "24911" and old.exists()  # copied, so an older agent keeps the same seed
    new.write_text("33333\n")
    adopt_legacy_seed(tmp_path)
    assert new.read_text() == "33333\n"  # the wrapper's seed is never overwritten


def test_wrapper_internals_we_use_exist():
    # Loose pin (<0.7): this only catches a rename when CI runs, it doesn't protect published installs.
    from cloakbrowser.human import Human
    from cloakbrowser.human.world import Worlds
    assert all(hasattr(Human, n) for n in ("ensure_cursor", "press_mouse", "_wheel_burst"))
    assert hasattr(Worlds, "evaluate")


def test_mcp_format_shows_probabilities_alternatives_and_stale():
    from cloak_agent.mcp_server import _format
    result = {
        "status": "done", "detail": None, "url": "https://x", "title": "T", "actions": 1, "jev_calls": 2,
        "elapsed_ms": 10, "markdown": "md", "stale": ["120ms click 'A': moved []"],
        "trace": [{"step": 1, "kind": "click", "action": "CloakHQ/CloakBrowser", "text": None,
                   "probability": 0.94, "alternatives": [("GitHub Projects on X", 0.05)]}],
    }
    out = _format("t1", result)
    assert "1. click 'CloakHQ/CloakBrowser'  p=0.94 ['GitHub Projects on X' p=0.05]" in out
    assert "stale retries" in out and "moved" in out
    assert out.index("</untrusted_page_content>") > out.index("md")


def test_resolve_maps_snapshot_indices_to_observed_actions_only():
    _, targets, controls = model.page_view({"url": "u", "title": "t", "text": "", "actions": ACTIONS}, [])
    assert model.resolve(targets, controls, "TYPE_TEXT", "1")["id"] == "e1"
    assert model.resolve(targets, controls, "SELECT", "3:1")["value"] == "price"
    assert model.resolve(targets, controls, "SCROLL_DOWN")["delta"] == 560
    with pytest.raises(ValueError, match=r"\['1', '2'\]"):  # lists the valid targets
        model.resolve(targets, controls, "CLICK", "9")
    with pytest.raises(ValueError):  # DONE/BLOCKED are Jev's verdicts, not something to execute
        model.resolve(targets, controls, "DONE")


class FakeStepSession:
    def __init__(self, stale=False):
        self.acted, self.stale = [], stale

    async def act(self, page, state, action, text=None):
        self.acted.append((action["id"], text))
        if self.stale:
            from cloak_agent.browser import StalePage
            raise StalePage("Page changed since this decision.")

    async def observe(self, page, after=None):
        return {**STATE, "marker": "after"}


STATE = {"url": "u", "title": "t", "text": "", "actions": ACTIONS, "marker": "before"}


def test_step_target_mode_types_caller_text_without_a_model(monkeypatch):
    from cloak_agent import agent

    async def no_model(*_):
        raise AssertionError("target mode must not call a model")
    monkeypatch.setattr(agent, "field_text", no_model)
    monkeypatch.setattr(agent, "choose", no_model)
    s = FakeStepSession()
    r = asyncio.run(agent.step(s, None, state=STATE, op="type_text", target="1", text="cats"))
    assert s.acted == [("e1", "cats")] and r["executed"] and r["page_changed"] and r["text"] == "cats"
    with pytest.raises(ValueError, match="needs text"):
        asyncio.run(agent.step(s, None, state=STATE, op="TYPE_TEXT", target="1"))
    with pytest.raises(ValueError, match="snapshot first"):
        asyncio.run(agent.step(s, None, op="CLICK", target="2"))
    stale = FakeStepSession(stale=True)
    with pytest.raises(Exception, match="Page changed"):
        asyncio.run(agent.step(stale, None, state=STATE, op="CLICK", target="2"))
    assert stale.acted == [("e3", None)]  # tried once, never retried


def test_step_instruction_mode_done_executes_nothing(monkeypatch):
    from cloak_agent import agent

    async def done(state, goal, history):
        return {"operation": "DONE", "action": {"id": "DONE", "kind": "done", "label": "DONE"}}
    monkeypatch.setattr(agent, "choose", done)
    s = FakeStepSession()
    r = asyncio.run(agent.step(s, None, instruction="accept cookies"))
    assert not r["executed"] and s.acted == []


def test_mcp_browser_use_rejects_unknown_and_busy_tabs_and_releases_activity():
    from cloak_agent import mcp_server

    class Page:
        url = "https://x"

        def is_closed(self):
            return False

    class FakeSession:
        closed = False

        def is_gone(self, page):
            return getattr(page, "crashed", False)

    async def scenario():
        b = mcp_server.Browser()
        b.session = FakeSession()
        with pytest.raises(mcp_server.ToolError, match="is gone"):
            async with b.use("t9"):
                pass
        b.tabs["t1"] = tab = mcp_server.Tab(Page())
        async with tab.lock:  # a browse call holds the tab
            with pytest.raises(mcp_server.ToolError, match="busy"):
                async with b.use("t1"):
                    pass
        with pytest.raises(mcp_server.ToolError, match="RuntimeError: boom") as e:
            async with b.use("t1"):
                raise RuntimeError("boom")
        assert "tab_id: t1" in str(e.value) and b.active == 0 and not tab.lock.locked()
        async with b.use("t1"):  # a call that ends normally on a tab that crashed during it
            tab.page.crashed = True
        assert "t1" not in b.tabs
        b._idle_task.cancel()

    asyncio.run(scenario())


def test_mcp_view_is_what_jev_sees_and_fences_page_content():
    from cloak_agent.mcp_server import _format_view
    view, _, _ = model.page_view(STATE, [])
    out = _format_view(STATE)
    assert all(line in out for line in view["elements"])
    assert "operations: TYPE_TEXT, CLICK, SELECT, SCROLL_DOWN, WAIT" in out
    assert out.index("<untrusted_page_content>") < out.index(view["elements"][0])


def test_resolve_redirects_decodes_clear_text_google_links_without_network():
    md = "### [A](https://www.google.com/url?q=https://a.example/x&sa=U) and [B](https://b.example/)"
    out = asyncio.run(model.resolve_redirects(md))
    assert out == "### [A](https://a.example/x) and [B](https://b.example/)"


def test_split_chunks_at_headings_and_size():
    md = "intro\n## [A](https://a)\nsnippet a\n## B\n" + "x" * 50 + "\n\n" + "y" * 50
    chunks = model.split_chunks(md, limit=60)
    assert chunks[0] == "intro" and chunks[1].startswith("## [A]") and "snippet a" in chunks[1]
    assert all(len(c) <= 60 for c in chunks)
