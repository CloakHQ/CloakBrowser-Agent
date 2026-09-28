# CloakBrowser Agent: a Jev-powered stealth browser agent

**Give it a goal in plain language. [TypeSafe Jev](https://docs.typesafe.ai/introduction) decides every step in ~0.3 s, [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) carries it out like a human, and you get the result back as markdown.**

```text
browse(goal="Find and open the Wikipedia article about Gödel's incompleteness theorems.",
       url="https://en.wikipedia.org/wiki/Main_Page")

status: done
url: https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems
title: Gödel's incompleteness theorems - Wikipedia
actions taken: fill 'Search Wikipedia' = "Gödel's incompleteness theorems" → click "Gödel's incompleteness theorems Limitative results"
<untrusted_page_content>
# Gödel's incompleteness theorems
Gödel's incompleteness theorems are two [theorems](https://en.wikipedia.org/wiki/Theorem) of [mathematical logic](https://en.wikipedia.org/wiki/Mathematical_logic) ...
```

It works as an **MCP server** (Claude Code, Cursor, Claude Desktop, any MCP client), a **CLI**, or a **Python library**.
It runs on [CloakBrowser](https://github.com/CloakHQ/CloakBrowser), a stealth Chromium with human-like mouse and keyboard input.

## Why Jev

Most browser agents ask a large language model to *write* the next action, which costs seconds per step.
[Jev](https://docs.typesafe.ai/introduction) is TypeSafe's first **System One** model. It doesn't generate text: you give it the current state plus typed questions, and it returns an answer with calibrated probabilities.
A browser step is exactly that kind of question: *which of these controls, doing what?*

- **One request per step.** Jev answers "which operation" and "which element" together in one request (speculative fan-out: a target question for each possible operation, and only the chosen one is used).
- **Fast.** In our runs, Jev decisions averaged 0.28–0.43 s each, about 1 s for a whole search task. A text model is called only when a field needs typing.
- **Probabilities, not prose.** Every decision comes with a distribution and a confidence, so the code can see when the model is unsure.
- **Nothing to parse.** Answers are typed choices from options we built, so there's no free-form output to go wrong.
- **Jev ranks the result too.** When the task is done, Jev scores every section of the final page against the goal, and only the relevant sections come back.

## How it works

```text
goal ─► OBSERVE  read the page → numbered table of the controls a user can actually reach
          ▲
          │  DECIDE  one Jev request: which operation (CLICK, TYPE_TEXT, SELECT, SCROLL, WAIT, DONE, BLOCKED)
          │          and which element, answered together
          │          TYPE_TEXT → a small text model writes only the value for that one field
          │
          └─ ACT     human-like click / typing, after checking the page did not change
DONE → the page is turned into markdown, Jev scores each section against the goal, the best sections are returned
```

- **No site-specific code.** The same loop runs on every site and re-plans from the current page after every action. Cookie walls, popups and changed layouts are just more elements to choose from.
- **Model output never becomes code.** The model picks from indices we built. It never writes selectors, coordinates or scripts.
- **Nothing is injected into the page.** The page is read without adding anything to it that the site's scripts can see.
- **Only reachable controls are offered.** Hidden, disabled, and covered elements (for example, behind a modal) aren't in the table.
- **A DONE answer isn't taken as proof.** Check results that matter.

## Requirements

- Python 3.10+
- A [TypeSafe API key](https://docs.typesafe.ai/introduction) (Jev)
- A key for any OpenAI-compatible chat model (OpenRouter, OpenAI, DeepSeek, …). It is used only to write field values.
- A [CloakBrowser](https://cloakbrowser.dev) license key for the latest stealth build. **A free key takes one GitHub sign-in:** run `cloakbrowser login` or go to [cloakbrowser.dev/free](https://cloakbrowser.dev/free). A free key allows one browser session at a time, and a paid key raises that limit. Without any key, the older build is used.

The browser binary downloads automatically on first use. Node is not needed.

## Install

Not on PyPI yet. From source:

```bash
git clone <this repo> cloakbrowser-agent && cd cloakbrowser-agent
uv venv && uv pip install -e .        # or: python -m venv .venv && .venv/bin/pip install -e .
```

## Configure

| Variable | Required | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | yes | Jev decisions |
| `TYPESAFE_MODEL` | no | default `jev-latest` |
| `TEXT_MODEL_BASE_URL` | yes | OpenAI-compatible base URL, e.g. `https://openrouter.ai/api/v1` |
| `TEXT_MODEL_API_KEY` | yes | key for that endpoint |
| `TEXT_MODEL` | yes | model id, e.g. a small fast model |
| `TEXT_MODEL_REASONING` | no | sent as `reasoning_effort` (`low` / `medium` / `high`); `none` omits it |
| `TEXT_MODEL_HEADERS` | no | JSON object of extra request headers, if your provider needs any |
| `CLOAKBROWSER_LICENSE_KEY` | recommended | CloakBrowser license key (`cb_...`). Instead of setting it here, you can run `cloakbrowser login` once: the saved key is picked up automatically |

## Use as an MCP server

**Claude Code**
```bash
claude mcp add cloak-agent --scope user \
  -e TYPESAFE_API_KEY=... \
  -e TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1 -e TEXT_MODEL=... -e TEXT_MODEL_API_KEY=... \
  -e CLOAKBROWSER_LICENSE_KEY=cb_... \
  -- /path/to/cloakbrowser-agent/.venv/bin/cloak-agent-mcp
```

**Cursor / Claude Desktop** (`mcpServers` in the client's config)
```json
{
  "mcpServers": {
    "cloak-agent": {
      "command": "/path/to/cloakbrowser-agent/.venv/bin/cloak-agent-mcp",
      "env": {
        "TYPESAFE_API_KEY": "...",
        "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "TEXT_MODEL": "...",
        "TEXT_MODEL_API_KEY": "...",
        "CLOAKBROWSER_LICENSE_KEY": "cb_..."
      }
    }
  }
}
```

### Tools

**`browse(goal, url?, tab_id?)`** runs one task and returns:
- `status`: `done`, `blocked`, `needs_input` (the goal lacks a value a field needs), `budget` (step limit reached), or `error`
- `tab_id`, the final URL and title, the actions taken
- the relevant page content as markdown, fenced as `<untrusted_page_content>`

Tips:
- Put every value the task needs into the goal (search terms, form values), because fields are filled from the goal.
- To continue on the same page, e.g. after `needs_input` or for a follow-up step, call `browse` again with the returned `tab_id`.

**`close_tab(tab_id)`** closes a tab.

### Browser lifecycle

- The browser starts on the first call, headed by default so you can watch it.
- Tabs stay open after a task so you can see the result.
- After `CLOAK_AGENT_IDLE_MINUTES` without calls (default 5), the browser closes itself along with its tabs, and it relaunches on the next call. It also closes when the MCP server stops.
- An open browser holds one CloakBrowser session. On a free key (one session), close it or let it idle out before running another CloakBrowser script.

| Variable | Default | Meaning |
|---|---|---|
| `CLOAK_AGENT_IDLE_MINUTES` | `5` | close the browser after this long without calls |
| `CLOAK_AGENT_HEADLESS` | off | `1` = run headless |
| `CLOAK_AGENT_HUMANIZE` | on | `0` = instant clicks and typing (faster, less human-like) |
| `CLOAK_AGENT_PROFILE` | `~/.cloakbrowser-agent/profile` | persistent browser profile (cookies and consent choices survive) |
| `CLOAK_AGENT_CDP` | unset | attach to an already running browser, e.g. `http://127.0.0.1:9222` |

A profile can only be open in one browser at a time. Give each instance its own `CLOAK_AGENT_PROFILE`.

## Use from the command line

```bash
export TYPESAFE_API_KEY=... TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1 TEXT_MODEL=... TEXT_MODEL_API_KEY=...
export CLOAKBROWSER_LICENSE_KEY=cb_...     # or run `cloakbrowser login` once

# one task, own browser (closes at the end; add --keep-open to inspect)
cloak-agent run --url https://en.wikipedia.org --goal "Open the Wikipedia article about Gödel's incompleteness theorems."

# debugging: keep one headed browser running, then run tasks against it
cloak-agent browser --port 9222
cloak-agent run --cdp http://127.0.0.1:9222 --url https://www.google.com --goal "Search Google for 'CloakBrowser'"
```

`--no-humanize` switches to instant input.

## Use from Python

Uses the same environment variables as above, including `CLOAKBROWSER_LICENSE_KEY` (or the key saved by `cloakbrowser login`).

```python
import asyncio
from cloak_agent import Session, run

async def main():
    session = await Session.launch(headless=False)        # or: await Session.connect("http://127.0.0.1:9222")
    page = await session.new_tab("https://en.wikipedia.org")
    result = await run(session, "Open the Wikipedia article about Gödel's incompleteness theorems.", page=page)
    print(result["status"], result["url"])
    print(result["markdown"])
    await session.close()

asyncio.run(main())
```

## Speed

Most of a humanized run is spent moving the mouse and typing at a human pace. That's on purpose, because behavior is scored on protected sites.

| Task (single runs, one machine) | Humanized | `--no-humanize` |
|---|---|---|
| Google search → results | 11.4 s | 3.4 s |
| Wikipedia search → open article | ~12 s | — |

Jev decisions take ~0.3 s each, roughly 1 s per task. Filling a field with the text model adds 0.5–2 s depending on the model.

## Limits

- Frames, closed shadow roots, canvas apps, file uploads, links that open new tabs, and CAPTCHAs aren't handled.
- Native `<select>` values are set programmatically, not through a mouse-driven dropdown.
- Autocomplete: after typing, it may pick a close suggestion instead of submitting the exact text.
- Password fields are never read or offered to the model.
- A valid action can still be the wrong one, and `done` means the model saw the goal as met. Check results that matter.

## Security

- Page content is untrusted. It's returned fenced as `<untrusted_page_content>` so the calling agent doesn't treat it as instructions.
- API keys stay in the server's environment and are only sent to the endpoints you configure.
- For Google result links, the real target is resolved with one direct request per link, since Google hides it.

## Credits

The observe → choose → act loop, the element snapshot and the Jev instructions are adapted from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT, see `LICENSE-jev-ultrafast`).
