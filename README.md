# claude-usage-diff

A local, zero-dependency dashboard that compares your **official Claude account usage** (the 5-hour / 7-day rate limits) against **your own local token activity**, so you can tell whether a usage spike is coming from you or from someone else sharing the same account.

Everything runs on your machine. No API key, no telemetry, no data leaves your computer.

![status](https://img.shields.io/badge/dependencies-python%20stdlib%20only-blue)

## Why

If you share a Claude account (e.g. with a friend or teammate), the built-in rate-limit indicator only tells you the current percentage — not *whose* usage is driving it. This tool logs official usage snapshots over time and correlates them against your own local transcript activity, so a chart like "official usage is climbing but my own activity is flat" becomes a visible signal instead of a guess.

## Features

- **① Official 5h usage** — bar chart, one bar per 5 minutes (matching the fastest interval Claude Code itself refreshes its usage cache), with a staleness warning when the recorded value hasn't updated even though its own window should already have reset.
- **② Local token usage** — your own token consumption per time bucket (rate), overlaid with a second series estimating cumulative usage since the current official window started, converted to an estimated percentage (see [Calibration caveats](#calibration-caveats) — treat this second series as a rough order-of-magnitude guide, not a precise number).
- **③ Per-conversation breakdown** — a bar per conversation (not just per project folder), labeled with Claude Code's own AI-generated conversation title. Subagent runs are attributed back to the parent conversation that spawned them instead of collapsing into one opaque bucket. Bars that span an official-usage reset boundary are split two-color (usage before the boundary vs. after).
- **Efficient by design** — the recording side throttles duplicate writes so a long-idle terminal window doesn't spam the history file with thousands of identical snapshots.

## How it works

Two independent pieces:

- **`claude_usage_diff.py`** — a [statusLine](https://code.claude.com/docs/en/statusline) command. Claude Code invokes it on an interval and passes session/usage info on stdin; it appends a snapshot to `official_history.jsonl`. It prefers Claude Code's own account-wide usage cache (`~/.claude.json`'s `cachedUsageUtilization`) over the per-session `rate_limits` block Claude Code passes on stdin, because the per-session block only refreshes when *that specific session* gets a new API response — an idle window otherwise reports a frozen, increasingly stale number forever.
- **`dashboard.py`** — a small local HTTP server (`http://127.0.0.1:8766`, bound to localhost only) that reads `official_history.jsonl` plus your `~/.claude/projects` transcripts on every request and renders the charts as inline SVG. No JS, no build step, no CDN.

## Installation

Requires Python 3.9+ (standard library only — nothing to `pip install`).

1. Clone this repo somewhere permanent, e.g.:
   ```bash
   git clone <this-repo-url> ~/.claude/usage-diff
   ```
2. Wire up the statusLine hook in your Claude Code settings (`~/.claude/settings.json`):
   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "python3 ~/.claude/usage-diff/claude_usage_diff.py",
       "refreshInterval": 5
     }
   }
   ```
   This is what makes the data collection happen automatically — no manual step needed after this. Note: this only fires for interactive terminal sessions; a headless/SDK-driven session may not invoke it.
3. Start the dashboard:
   ```bash
   python3 ~/.claude/usage-diff/dashboard.py
   ```
   Then open `http://127.0.0.1:8766` in a browser. The page auto-refreshes every 10 seconds.

To keep it running in the background:
```bash
nohup python3 ~/.claude/usage-diff/dashboard.py > /tmp/usage-dashboard.log 2>&1 &
```

## Configuration

All via environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_USAGE_DIFF_PORT` | `8766` | Dashboard HTTP port |
| `CLAUDE_USAGE_DIFF_WINDOW_HOURS` | `5` | How far back the charts look |
| `CLAUDE_USAGE_DIFF_BUCKET_MIN` | `10` | Bucket size (minutes) for the local-usage chart |
| `CLAUDE_USAGE_STATE_DIR` | `~/.claude/usage-diff` | Where `official_history.jsonl` is stored/read |
| `CLAUDE_USAGE_WINDOW_MINUTES` | `10` | Window used by the statusLine's own on-screen rate calculation |
| `CLAUDE_USAGE_STALE_SECONDS` | `180` | Minimum gap between duplicate-value writes to the history file |

Example — 12-hour window, 30-minute buckets:
```bash
CLAUDE_USAGE_DIFF_WINDOW_HOURS=12 CLAUDE_USAGE_DIFF_BUCKET_MIN=30 python3 dashboard.py
```

## Maintenance

Essentially none:

- Data accumulates automatically from normal Claude Code use — nothing to run manually.
- To reset history: delete `official_history.jsonl` in your state dir; it's recreated on the next snapshot.
- To pick up a config change: just restart `dashboard.py`.
- To uninstall: delete the directory and remove the `statusLine` block from `settings.json`.

## Calibration caveats

Chart ②'s right axis (cumulative usage → estimated %) needs a tokens-per-percentage-point constant (`TOKENS_PER_PCT` in `dashboard.py`). This was derived empirically by watching a real account over a controlled window, but **it is not a stable physical constant** — official usage accounting appears to be cost/model-weighted (input, output, cache-read, and cache-creation tokens are priced very differently, and different models have very different per-token cost), not a flat token count. In testing, two consecutive single-user sub-periods produced tokens-per-percent ratios that differed by **15x**. Treat that axis as an order-of-magnitude guide, and recalibrate against your own account if you rely on it — see the constant's comment in `dashboard.py` for the full methodology and numbers.

Similarly, the ③ split-bar boundary assumes `resets_at` marks a fixed, discrete window start/end. That's the natural reading of the field, but hasn't been independently confirmed against Anthropic's actual accounting model (e.g. a sliding/decaying window would behave differently).

## Privacy

Nothing here calls any network API. `official_history.jsonl` and your Claude Code transcripts (read for the local-usage charts) both stay on your machine and are never included in this repo — see `.gitignore`.

## License

MIT — see `LICENSE`.
