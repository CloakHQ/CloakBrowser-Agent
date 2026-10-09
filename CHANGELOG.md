# Changelog

All notable changes to cloakbrowser-agent are documented here.

---

## [0.1.4] — 2026-10-09

- Works with cloakbrowser 0.6 and its new humanize engine. The dependency is now `cloakbrowser[geoip]>=0.6.0,<0.7` (was `==0.5.11`). Mouse movement, clicks, typing and scrolling stay humanized.
- Select-all before typing into a field follows the browser persona (Meta on a macOS persona, Control otherwise), like the wrapper.
- The fingerprint seed per profile is now kept by cloakbrowser itself (`.cloakbrowser-seed` in the profile). A seed saved by an earlier agent version is carried over on the next launch, so existing profiles keep the same identity.
- New MCP tools `snapshot` and `act` to inspect a tab and take it over:
  - `snapshot(tab_id, screenshot?)` returns the page exactly as the agent sees it (element table, visible text), with an optional viewport image.
  - `act(tab_id, ...)` performs one step, either by element index (no model call) or from a plain-language instruction (one decision), and returns the new view. A page that changed since the snapshot is never acted on.
- Text clipped by its container (e.g. calendar months outside a date picker) no longer counts as visible content.
- The page state includes how far the page is scrolled, and the agent scrolls to a control that is off-screen instead of giving up. A full-page consent wall is now scrolled to and rejected.
- Date pickers: the agent uses the picker's Next/Previous buttons when the requested date is not shown.

## [0.1.3] — 2026-09-29

- Consent walls are rejected again (0.1.2 could click Accept). Target choices get their own short rules: reject or close consent and overlays, pick the matching autocomplete suggestion, confirm a date after picking it, don't toggle what is already set.
- Results are read once the page has stopped changing for 1 s (up to 4 s), so progressively loading pages (e.g. flight results) return their full content.

## [0.1.2] — 2026-09-29

- Smaller decision requests (about 30-45% fewer tokens) with the same decisions: rules are sent once, and each element is one compact line with a short legend.

## [0.1.1] — 2026-09-29

- Navigation loops stop: the agent sees where each past step led, and reports `blocked` when it would repeat a path that already failed.
- A browser or tab closed (or crashed) mid-task ends with a clean `error` status and the partial trace. The MCP server relaunches the browser on the next call.
- One fingerprint seed per profile, kept across launches.
- `geoip` is on: timezone, locale and WebRTC IP follow the exit IP. Depends on `cloakbrowser[geoip]`.
- `CLOAK_AGENT_PROXY` sets a proxy for the MCP browser.

## [0.1.0] — 2026-09-29

First release.

- Goal-driven browser agent on CloakBrowser: Jev picks each step (operation and target), CloakBrowser carries it out with humanized input, reading the page only from an isolated world.
- Fields are filled by a text model on any OpenAI-compatible endpoint.
- Launch mode or connect to a running browser over CDP. `cloak-agent browser` keeps a headed browser open; `--no-humanize` for instant input.
- Open shadow roots are read, and covered elements are not offered.
- Result: the page as markdown, the relevant parts ranked by Jev, Google redirect links resolved.
- MCP server `cloak-agent-mcp` with `browse` and `close_tab`. Each step is shown with its probability and runner-ups. The browser closes after `CLOAK_AGENT_IDLE_MINUTES` idle and relaunches on demand.
- Released to PyPI from a `v*` tag.
