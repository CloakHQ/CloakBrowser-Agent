"""Jev picks operation + target; an OpenAI-compatible text model writes field values.

action_space/choose/validate_choice are adapted from browser-use/jev-ultrafast model.py
(MIT, Copyright (c) 2026 Browser Use; see LICENSE-jev-ultrafast).
"""

import asyncio
import json
import math
import os
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.AsyncClient(timeout=60)
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
MAX_RANKED_BLOCKS = 30


class NeedsInput(Exception):
    """The goal does not contain a value this field requires; the caller must supply it."""


async def post_json(url, key, body, headers=None):
    for attempt in range(3):
        try:
            response = await CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}", **(headers or {})})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Model connection failed ({type(e).__name__}); no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            await asyncio.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}: {response.text[:300]}")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


async def choose(page, goal, history):
    elements, targets, controls = action_space(page["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        DONE="Every requirement is visibly satisfied.",
        BLOCKED="No supported operation can progress, or the only moves left repeat a path already tried "
                "without success (see recent_actions url → led_to).",
    )
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: page[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                # url/led_to are ours (upstream sends only the first four): they let Jev see repeated circles.
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed", "url", "led_to")}
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = await post_json(TYPESAFE_URL, os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target, target_probabilities = None, {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate only the head the operation selects.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        target_probabilities = target_answer["probabilities"]
        action = targets[operation][target]
        probability = target_answer["probabilities"][target]
    else:
        action = controls.get(operation) or {"id": operation, "kind": operation.lower(), "label": operation}
        probability = operation_answer["probabilities"][operation]
    return {
        "action": action,
        "operation": operation,
        "target": target,
        "probability": round(probability, 3),
        "confidence": round(operation_answer["confidence"], 3),
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_probabilities,  # full distribution over the chosen operation's targets
        # Runner-ups (label, p) for debugging a wrong pick, e.g. why a similar-looking result won.
        "alternatives": [
            (targets[operation][i]["label"][:60], round(p, 3))
            for i, p in sorted(target_probabilities.items(), key=lambda kv: -kv[1])[1:3]
            if p >= 0.01
        ],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


async def field_text(context):
    """Any OpenAI-compatible endpoint: TEXT_MODEL_BASE_URL + TEXT_MODEL_API_KEY + TEXT_MODEL."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; the executor never guesses field text.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "openrouter/free")
    body = {
        "model": model,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": TEXT_VALUE}, {"role": "user", "content": json.dumps(context)}],
    }
    effort = os.environ.get("TEXT_MODEL_REASONING", "")
    if effort and effort != "none":
        body["reasoning_effort"] = effort
    headers = json.loads(os.environ.get("TEXT_MODEL_HEADERS") or "{}")
    started = time.perf_counter()
    result = await post_json(base + "/chat/completions", key, body, headers)
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or (value is not None and (not isinstance(value, str) or len(value) > 2000)):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text model returned no valid field value; nothing typed.") from None
    if value is None or not value.strip():
        raise NeedsInput(context["field"]["label"])
    return value, {"model": model, "latency_ms": round((time.perf_counter() - started) * 1000)}


def split_chunks(markdown, limit=1500):
    """Split page markdown at headings; oversized sections split again at blank lines."""
    chunks, current = [], []
    for line in markdown.splitlines():
        if line.startswith("#") and current:
            chunks.append("\n".join(current).strip())
            current = []
        current.append(line)
    chunks.append("\n".join(current).strip())
    out = []
    for chunk in filter(None, chunks):
        while len(chunk) > limit:
            cut = chunk.rfind("\n\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            out.append(chunk)
    return out


GOOGLE_REDIRECT = re.compile(r"\((https://www\.google\.[a-z.]+/(?:goto|url)\?[^)\s]+)\)")


async def _resolve_redirect(url):
    params = parse_qs(urlparse(url).query)
    for key in ("q", "url"):  # classic /url?q=<real> carries the target in clear text
        if params.get(key, [""])[0].startswith("http"):
            return params[key][0]
    try:  # /goto?url=<opaque> only resolves server-side: one GET, redirect not followed
        response = await CLIENT.get(url, follow_redirects=False, timeout=5)
    except httpx.HTTPError:
        return url
    return response.headers.get("location", url) if response.is_redirect else url


async def resolve_redirects(markdown):
    """Replace Google search redirect wrappers with the real target URLs."""
    urls = list(dict.fromkeys(GOOGLE_REDIRECT.findall(markdown)))
    for url, real in zip(urls, await asyncio.gather(*map(_resolve_redirect, urls))):
        markdown = markdown.replace(f"({url})", f"({real})")
    return markdown


async def rank_blocks(goal, blocks):
    """One Noul per block in a single request: does this block serve the goal? Returns probabilities."""
    blocks = blocks[:MAX_RANKED_BLOCKS]
    if not blocks:
        return []
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {"goal": goal, "blocks": blocks},
        "questions": {
            f"b{i}": {
                "type": "noul",
                "instructions": f"Does `blocks[{i}]` contain information that answers or fulfils `goal`?",
            }
            for i in range(len(blocks))
        },
    }
    result = await post_json(TYPESAFE_URL, os.environ["TYPESAFE_API_KEY"], body)
    return [result["answers"][f"b{i}"]["noul"] for i in range(len(blocks))]
