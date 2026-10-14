# Changelog

All notable changes to cloakbrowser-agent are documented here.

---

## [0.1.6] — 2026-10-14

- **DONE is checked a second time.** When the agent picks DONE, Jev is asked about the page both ways ("is the goal achieved?" and "is it NOT achieved yet?"). If they disagree, the task ends as the new status `done_unverified`, so the caller can look at the page before trusting it. The output shows a `done check:` line.
- **Loop brake.** The same action on the same page a 3rd time ends the task as `blocked`. This catches cycles that change the page on every step, which the "no change" stop can't see.
- **Logins without exposing the password.** `browse(secrets={"password": "env:SHOP_PW"})` (MCP, `env:` references only) or `run(secrets={...})` (Python). Jev picks which secret fits each field, the value is typed locally, and every model request, step, page content and later `snapshot`/`act` reply shows `[secret:name]` instead. Password fields are now offered with a masked value. A password field with no matching secret ends as `needs_input`; the text model never writes a password.
- **Date and time fields** (`date`, `time`, `datetime-local`, `month`, `week`) are now offered, with `type=` in the element table. The text model writes the standard format (`18:30`, `2026-10-28`) and the value is set on the field directly.
- Each step shows Jev's probability for the operation and its runner-up next to the target `p`: `op=CLICK 0.43 (SCROLL_DOWN 0.35)`. In our tests, wrong clicks showed up in a low `op` more than in `p`.
- Results and snapshots note when elements were left out by the 250-element cap.
- A typed value is reused only for the same field (two same-labeled fields no longer share one after a retry).
- A model provider error sent with HTTP 200 now surfaces with the provider's message.

## [0.1.5] — 2026-10-14

- Requires cloakbrowser 0.6.3 or newer (`cloakbrowser[geoip]>=0.6.3,<0.7`). Humanized typing, mouse moves and scrolling follow its new human timing: keys held and overlapping like a real typist, mouse moves that take longer the farther they go, and scrolling in whole mouse-wheel notches.
- A country missing from cloakbrowser's GeoIP locale map no longer fails the launch (falls back to `en-US`).

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
