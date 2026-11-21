# Morning Bar

A personal, local-first morning briefing for Linux. The collector requests only
public feeds and APIs, keeps compact article metadata in XDG state, and sends
public story metadata to Gemini only when a local API key is available.

## Project status

Phases A, B, C, and D are implemented: asynchronous feed/API collection, conditional
requests, URL normalisation, deduplication, clustering, local ranking,
page-change watching, dry-run CLI, Gemini ranking and summaries, structured
briefing JSON, local fallback, quota accounting, feedback, morning triggers, and
the standalone Quickshell pill/card. The CLI also supports reviewed Gemini topic
proposals with source URL verification. The approved
65-source list is installed in the active config. See
[`config/interests.proposal.toml`](config/interests.proposal.toml) for every
approved URL and [`config/source-review.md`](config/source-review.md) for the
coverage and validation notes.

## Development

Requires Python 3.12+ and `uv`.

```sh
uv sync --all-groups
uv run pytest
uv run morningbar run --dry-run --config tests/fixtures/interests.toml
uv run morningbar status
```

HTML page watchers request `robots.txt` first, follow ETag and Last-Modified
headers, and keep only a short extracted excerpt plus its hash for change
detection. A first check records a baseline but does not create a story. Page
deadlines remain unverified until the linked official notice confirms them.

Gemini uses the official `google-genai` SDK with `gemini-3.8-flash` and the
lighter `gemini-3.5-flash-lite` fallback. Model names and conservative
app-side request/token caps are editable under `[gemini]` in the interests
file. Google's actual limits vary by project and are shown in Google AI
Studio; these local caps are safeguards, not provider quotas. Free-tier input
content may be used by Google to improve its products. Requests include only
public headlines, links, short excerpts, source/time metadata, and the relevant
topic keywords. Interactions use `store=false` and omit user watchlists,
feedback, and weights.

To use the keyring, store a key locally from a terminal prompt so it is not
placed in shell history or chat:

```sh
read -r -s -p "Gemini API key: " mb_gemini_key
printf '\n'
printf '%s' "$mb_gemini_key" | secret-tool store --label="Morning Bar Gemini API" service morningbar key GEMINI_API_KEY
unset mb_gemini_key
```

The API key can instead be supplied through `GEMINI_API_KEY`, or kept in
`~/.config/morningbar/gemini-api-key` with mode `0600`. If the key is missing,
the API is offline, or the app-side usage caps are reached, the briefing uses
local ranking and shows `basic` mode.

Add a topic with one command:

```sh
morningbar topic add "robotics"
```

Gemini proposes a weight, keywords, and up to five public feed or page sources.
Morning Bar checks each URL, checks `robots.txt` before page requests, and shows
the full proposal. Only sources that pass verification are offered for saving;
the config changes only if you confirm at the prompt. The request uses the
configured Gemini model and local usage caps, and sends Gemini only the topic
label you entered. The generated TOML remains editable in
`~/.config/morningbar/interests.toml`.

```sh
morningbar run --no-llm
morningbar feedback STORY_ID --vote up
morningbar feedback STORY_ID --less-like
```

`--dry-run` uses local ranking and does not write a briefing or mark stories as
shown. A normal `run` writes
`~/.local/state/morningbar/briefing-YYYY-MM-DD.json` with its mode, headline,
TL;DR, summaries, importance, and source links.

The live config belongs in `~/.config/morningbar/interests.toml`. State,
briefings, SQLite usage, feedback, and conditional-request metadata belong in
`~/.local/state/morningbar/`.

## Morning trigger

The active config sets a local 05:00–11:00 window in
`Africa/Johannesburg`. `late_grace_minutes`, `trigger_debounce_seconds`, and
`network_wait_seconds` can be changed in `[settings]`. The user-level units are
installed under `~/.config/systemd/user/`:

- `morningbar-trigger.service` runs the single gated `morningbar trigger`
  command with nice 10, idle I/O, and a 20% CPU quota.
- `morningbar-trigger.timer` is persistent and scheduled for 05:00 local time.
- `morningbar-sleep-watch.service` watches logind's
  `PrepareForSleep=false` signal while the graphical session is active and
  requests the same trigger service. It currently uses about 6 MB RSS.

The graphical-session target, resume watcher, and timer are enabled for this
user. A flock prevents simultaneous starts; an atomic local trigger-state file
records the last start and completed local date. NetworkManager connectivity is
checked with backoff for up to the configured wait. If the network stays down,
the run uses only cached articles and local ranking. A metered NetworkManager
connection reduces the fetch-body budget to at most 1 MB, the feed item cap to
25, and candidate count to 100. Timer, graphical-session, and resume starts
all pass through the same time/day/lock/debounce gates.

Check the setup with:

```sh
morningbar trigger
morningbar status
systemctl --user list-timers morningbar-trigger.timer
journalctl --user -u morningbar-trigger.service -n 30
journalctl --user -u morningbar-sleep-watch.service -n 30
```

Outside the configured window, `morningbar trigger` exits without contacting
sources. `morningbar run` remains the manual collection command. No full live
collector run was made against the 65-source list.

## Morning Bar UI

The UI is a separate Quickshell config under `~/.config/quickshell/morningbar`;
it does not load or change the end-4 config. The user-level
`morningbar-ui.service` is on-demand and starts `qs -c morningbar`. The
accepted morning trigger starts it before network collection, so the pill can
show gathering progress while the briefing is built. The panel stays hidden
until the session is unlocked and while a fullscreen toplevel is active. On
Hyprland it checks `hyprctl locked`, which reports the Wayland session-lock
state used by end-4's lock screen; this shell does not update logind's
`LockedHint`. Niri and generic Wayland sessions use
`loginctl show-session "$XDG_SESSION_ID" -p LockedHint --value`. Lock state is
polled once per second and fails closed if the query is unavailable. Fullscreen
state uses Wayland's foreign-toplevel protocol. The compositor-specific checks
are kept in
[`CompositorAdapter.qml`](ui/quickshell/morningbar/CompositorAdapter.qml).

Summon the pill or expanded card manually:

```sh
morningbar show
morningbar show --expand
```

Expanded-card keys: `j`/`k` or up/down moves stories, left/right or `Tab`
changes category, `Enter` opens the selected link, `Esc` collapses, `d`
dismisses for today, and `r` starts another low-priority run. Story actions
record feedback or save the story in SQLite. Clicking the compact pill opens
the card without taking keyboard focus. After dismissing, `morningbar show`
clears today's dismissal.

For the installed Hyprland `0.57.2` Lua config, the optional blur rule is:

```lua
hl.layer_rule({ match = { namespace = "morningbar" }, blur = true })
```

Example manual summon binds (choose an unused key combination):

```lua
-- Hyprland 0.55+ Lua config
hl.bind("SUPER + SHIFT + M", hl.dsp.exec_cmd("/home/nolan-archie/Projects/morningbar/.venv/bin/morningbar show --expand"))
```

```kdl
// Niri config
binds {
    Super+Shift+M { spawn "/home/nolan-archie/Projects/morningbar/.venv/bin/morningbar" "show" "--expand"; }
}
```

No compositor config was edited. On this machine, the end-4 lock was invoked
through its existing Quickshell global and `hyprctl locked` changed to `true`
while logind's `LockedHint` remained `no`; the adapter uses the Hyprland signal
for this session. Niri is not installed, so its runtime behavior has not been
exercised. Niri windowed-fullscreen also marks the toplevel as fullscreen, so
that mode hides the pill too.

The verified UI states are stored in `ui/screenshots/` (capture made with
`grim`). The previews use clearly synthetic stories and are not news results.
When Quickshell cannot start, the CLI sends a desktop notification with up to
three stories and writes `~/.local/state/morningbar/briefing.html` for the
local fallback view. `MORNINGBAR_REDUCED_MOTION=1` forces transition and pulse
animations off; otherwise the UI reads GNOME's `enable-animations` preference.

The source review document records a mistake in the URL validation helper;
Samsung's robots-disallowed firmware page has been excluded.
