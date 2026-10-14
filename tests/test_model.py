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


def test_date_time_fields_show_their_type_to_jev_and_the_text_model():
    time_field = {"id": "e1", "node": 7, "kind": "fill", "role": "textbox", "label": "Preferred delivery time",
                  "value": "", "input_type": "time"}
    elements, targets, _ = model.action_space([time_field])
    assert model.element_line(elements[0]) == '[1] textbox "Preferred delivery time" type=time ops=TYPE_TEXT'
    assert model.field_context("goal", time_field, STATE, [])["field"]["input_type"] == "time"
    assert "input_type" not in model.field_context("goal", ACTIONS[0], STATE, [])["field"]


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
        "elapsed_ms": 10, "markdown": "md", "stale": ["120ms click 'A': moved []"], "omitted_max": 3,
        "trace": [{"step": 1, "kind": "click", "action": "CloakHQ/CloakBrowser", "text": None,
                   "probability": 0.94, "alternatives": [("GitHub Projects on X", 0.05)],
                   "op": "CLICK", "op_p": 0.43, "op_alt": [("SCROLL_DOWN", 0.35)]}],
    }
    out = _format("t1", result)
    assert ("1. click 'CloakHQ/CloakBrowser'  p=0.94 ['GitHub Projects on X' p=0.05]"
            " op=CLICK 0.43 (SCROLL_DOWN 0.35)") in out
    assert "stale retries" in out and "moved" in out
    assert "note: 3 elements on screen were not offered" in out
    assert "not offered" not in _format("t1", {**result, "omitted_max": 0})
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


class FakePage:
    url = "u"

    async def title(self):
        return "t"


class FakeRunSession:
    """run()'s view of a browser: each observe returns texts[i % len(texts)] (or a growing text)."""
    closed = False
    last_diff = ""

    def __init__(self, texts=None):
        self.texts, self.observed, self.acted = texts, 0, []

    async def observe(self, page, after=None):
        text = self.texts[self.observed % len(self.texts)] if self.texts else f"month {self.observed}"
        self.observed += 1
        return {**STATE, "text": text, "marker": f"m{self.observed}", "omitted_actions": 0,
                "scroll": {"y": 100 * self.observed, "height": 9000, "vh": 800}}

    async def fresh(self, page, state, action=None):
        return True

    async def act(self, page, state, action, text=None):
        self.acted.append((state["text"], action["label"], text))

    async def settled_markdown(self, page):
        return "md"

    def is_gone(self, page):
        return False


def decision(label, operation="CLICK", kind="click"):
    return {"action": {"id": label, "kind": operation.lower() if operation in {"DONE", "BLOCKED"} else kind,
                       "label": label},
            "operation": operation, "target": None, "probability": 0.9, "confidence": 0.9, "alternatives": [],
            "operation_probabilities": {operation: 0.9, "SCROLL_DOWN": 0.1}, "latency_ms": 1}


def run_fake(monkeypatch, session, pick, verified=True):
    from cloak_agent import agent

    async def choose(state, goal, history, **_):
        return pick(state, history)

    async def verify(state, goal, history):
        if verified == "raise":
            raise RuntimeError("Model provider returned HTTP 503")
        return {"achieved": 0.9 if verified else 0.55, "not_achieved": 0.1 if verified else 0.56,
                "verified": verified, "latency_ms": 1}
    monkeypatch.setattr(agent, "verify_done", verify)

    async def ranks(goal, chunks):
        return [1.0] * len(chunks)

    async def same(markdown):
        return markdown
    monkeypatch.setattr(agent, "choose", choose)
    monkeypatch.setattr(agent, "rank_blocks", ranks)
    monkeypatch.setattr(agent, "resolve_redirects", same)
    return asyncio.run(agent.run(session, "goal", page=FakePage()))


def test_loop_brake_stops_a_cycle_before_the_third_repeat(monkeypatch):
    s = FakeRunSession(["page A", "page B"])  # every click changes the page, so the no-change brake never fires
    r = run_fake(monkeypatch, s, lambda state, h: decision("X" if state["text"] == "page A" else "Y"))
    assert r["status"] == "blocked" and "3rd time" in r["detail"]
    assert [a[:2] for a in s.acted] == [("page A", "X"), ("page B", "Y"), ("page A", "X"), ("page B", "Y")]


def test_loop_brake_ignores_repeats_that_change_the_text(monkeypatch):
    s = FakeRunSession()  # "Next month": same click, new visible text each time
    r = run_fake(monkeypatch, s, lambda state, h: decision("DONE", "DONE") if len(h) == 5 else decision("Next"))
    assert len(s.acted) == 5 and r["status"] == "done" and r["done_check"]["verified"]


def test_done_check_disagreement_returns_done_unverified(monkeypatch):
    r = run_fake(monkeypatch, FakeRunSession(), lambda state, h: decision("DONE", "DONE"), verified=False)
    assert r["status"] == "done_unverified" and "0.56" in r["detail"] and r["jev_calls"] == 2


def test_verify_done_asks_straight_and_negated_in_one_request(monkeypatch):
    bodies = []

    async def fake_post(url, key, body, headers=None):
        bodies.append(body)
        return {"answers": {"achieved": {"noul": 0.55}, "not_achieved": {"noul": 0.56}}}
    monkeypatch.setattr(model, "post_json", fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    r = asyncio.run(model.verify_done(STATE, "goal", []))
    assert len(bodies) == 1 and set(bodies[0]["questions"]) == {"achieved", "not_achieved"}
    assert bodies[0]["state"]["goal"] == "goal" and not r["verified"]


def test_redact_replaces_values_longest_first_in_nested_data():
    secrets = {"user": "student", "pw": "student123"}
    out = model.redact({"a": ["hi student123", ("student",)], "n": 3}, secrets)
    assert out == {"a": ["hi [secret:pw]", ("[secret:user]",)], "n": 3}
    assert model.redact({"a": "x"}, None) == {"a": "x"}


def test_mcp_secrets_accept_only_env_references(monkeypatch):
    from cloak_agent.mcp_server import ToolError, resolve_secrets
    monkeypatch.setenv("SHOP_PW", "hunter22")
    assert resolve_secrets({"password": "env:SHOP_PW"}) == {"password": "hunter22"}
    assert resolve_secrets(None) is None
    with pytest.raises(ToolError, match="env:NAME reference"):
        resolve_secrets({"password": "hunter22"})
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ToolError, match="NOPE is not set"):
        resolve_secrets({"password": "env:NOPE"})


LOGIN = {"url": "https://x/home?user=student", "title": "t", "text": "Logged in as student", "omitted_actions": 0,
         "actions": [
    {"id": "e1", "node": 1, "kind": "fill", "role": "textbox", "label": "Password", "value": "", "secret": True},
    {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]}


def fake_jev(bodies, picks):
    """Answers every question of a request: noul 0.9 (not_achieved 0.1), choices from `picks` (by question id;
    a list is consumed one per request)."""
    async def post(url, key, body, headers=None):
        bodies.append(json.dumps(body))
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"noul": 0.1 if qid == "not_achieved" else 0.9}
                continue
            pick = picks[qid].pop(0) if isinstance(picks[qid], list) else picks[qid]
            answers[qid] = {"choice": pick, "confidence": 1.0,
                            "probabilities": {i: float(i == pick) for i in q["criteria"]}}
        return {"answers": answers}
    return post


class FakeLoginSession(FakeRunSession):
    async def observe(self, page, after=None):
        self.observed += 1
        return {**LOGIN, "marker": f"m{self.observed}"}

    async def settled_markdown(self, page):
        return "Welcome back, student"


def test_secret_is_typed_locally_and_never_reaches_a_model_or_the_result(monkeypatch):
    from cloak_agent import agent
    bodies, s = [], FakeLoginSession()
    monkeypatch.setattr(model, "post_json", fake_jev(bodies, {
        "operation": ["TYPE_TEXT", "DONE"], "type_text_target": "1", "secret": "password"}))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def no_text_model(*_):
        raise AssertionError("a secret field must not go to the text model")
    monkeypatch.setattr(agent, "field_text", no_text_model)
    secrets = {"username": "student", "password": "Pa55word!"}
    r = asyncio.run(agent.run(s, "log in", page=FakePage(), secrets=secrets))
    assert s.acted == [("Logged in as student", "Password", "Pa55word!")] and r["status"] == "done"
    assert r["trace"][0]["text"] == "[secret:password]"
    for leak in secrets.values():
        assert not any(leak in b for b in bodies) and leak not in json.dumps(r)
    assert any('"secrets_available": ["username", "password"]' in b for b in bodies)
    # the 2nd choose and the DONE check carry history whose url holds the username: redacted too
    assert sum('"recent_actions": [{' in b for b in bodies) >= 2


def test_done_check_failure_keeps_the_finished_task(monkeypatch):
    r = run_fake(monkeypatch, FakeRunSession(), lambda state, h: decision("DONE", "DONE") if h else decision("Submit"),
                 verified="raise")
    assert r["status"] == "done_unverified" and "503" in r["detail"] and len(r["trace"]) == 1


def test_loop_brake_lets_scrolling_continue_when_the_text_stays_the_same(monkeypatch):
    s = FakeRunSession(["same text"])  # image/canvas page: text constant, scroll position moves
    r = run_fake(monkeypatch, s, lambda state, h: decision("DONE", "DONE") if len(h) == 4
                 else decision("Scroll down", kind="scroll"))
    assert len(s.acted) == 4 and r["status"] == "done"


def test_snapshot_and_act_replies_hide_the_tabs_secrets(monkeypatch):
    from cloak_agent import agent, mcp_server
    tab = mcp_server.Tab(page=None, view={**LOGIN, "marker": "m"}, secrets={"username": "student"})
    out = asyncio.run(mcp_server._reply(None, "t1", tab, ["did: TYPE_TEXT 'Email' = 'student'"], False))
    assert "student" not in out and "[secret:username]" in out
    seen = []

    async def choose(state, goal, history, **_):
        seen.append(json.dumps(state))
        return decision("DONE", "DONE")
    monkeypatch.setattr(agent, "choose", choose)
    asyncio.run(agent.step(FakeLoginSession(), None, instruction="click Continue", secrets={"username": "student"}))
    assert seen and "student" not in seen[0]


def test_secret_field_without_secrets_needs_input(monkeypatch):
    from cloak_agent import agent
    monkeypatch.setattr(model, "post_json", fake_jev([], {"operation": "TYPE_TEXT", "type_text_target": "1"}))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")

    async def no_text_model(*_):
        raise AssertionError("the text model must never invent a password")
    monkeypatch.setattr(agent, "field_text", no_text_model)
    r = asyncio.run(agent.run(FakeLoginSession(), "log in", page=FakePage()))
    assert r["status"] == "needs_input" and "Password" in r["detail"]


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


class FakeTwoNamesSession(FakeRunSession):
    """Two empty fields with the same label: their text-model inputs are identical. The first fill goes stale."""
    async def observe(self, page, after=None):
        self.observed += 1
        return {"url": "u", "title": "t", "text": "form", "omitted_actions": 0, "marker": f"m{self.observed}",
                "actions": [
                    {"id": f"e{n}", "node": n, "kind": "fill", "role": "textbox", "label": "Name", "value": ""}
                    for n in (1, 2)]}

    async def act(self, page, state, action, text=None):
        from cloak_agent.browser import StalePage
        if not self.acted:
            self.acted.append(None)
            raise StalePage("changed")
        self.acted.append((action["node"], text))


def test_text_cache_does_not_cross_same_labeled_fields(monkeypatch):
    from cloak_agent import agent
    monkeypatch.setattr(model, "post_json", fake_jev([], {
        "operation": ["TYPE_TEXT", "TYPE_TEXT", "DONE"], "type_text_target": ["1", "2", "2"]}))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    values = iter(["John", "Jane"])

    async def text_model(context):
        return next(values), {"latency_ms": 1}
    monkeypatch.setattr(agent, "field_text", text_model)
    s = FakeTwoNamesSession()
    asyncio.run(agent.run(s, "fill the form", page=FakePage()))
    assert s.acted == [None, (2, "Jane")]  # field 2 gets its own value, not field 1's cached "John"


def test_post_json_raises_on_error_body(monkeypatch):
    import httpx
    body = {"error": {"message": "Upstream idle timeout exceeded", "code": 504}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    monkeypatch.setattr(model, "CLIENT", httpx.AsyncClient(transport=transport))
    with pytest.raises(RuntimeError, match="Upstream idle timeout exceeded"):
        asyncio.run(model.post_json("https://x.test", "k", {}))
