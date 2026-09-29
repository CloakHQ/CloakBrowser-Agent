import asyncio
import json

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


def test_profile_seed_is_stable_per_profile_and_recovers_from_junk(tmp_path):
    from cloak_agent.browser import profile_seed
    a, b = tmp_path / "a", tmp_path / "b"
    first = profile_seed(a)
    assert 10000 <= first <= 99999 and profile_seed(a) == first  # same profile → same seed
    assert (a / "cloak-agent-seed").read_text() == str(first)
    (b / "cloak-agent-seed").parent.mkdir(parents=True)
    (b / "cloak-agent-seed").write_text("junk")
    assert 10000 <= profile_seed(b) <= 99999  # unreadable file → a fresh valid seed


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


def test_resolve_redirects_decodes_clear_text_google_links_without_network():
    md = "### [A](https://www.google.com/url?q=https://a.example/x&sa=U) and [B](https://b.example/)"
    out = asyncio.run(model.resolve_redirects(md))
    assert out == "### [A](https://a.example/x) and [B](https://b.example/)"


def test_split_chunks_at_headings_and_size():
    md = "intro\n## [A](https://a)\nsnippet a\n## B\n" + "x" * 50 + "\n\n" + "y" * 50
    chunks = model.split_chunks(md, limit=60)
    assert chunks[0] == "intro" and chunks[1].startswith("## [A]") and "snippet a" in chunks[1]
    assert all(len(c) <= 60 for c in chunks)
