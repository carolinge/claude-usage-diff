#!/usr/bin/env python3
"""
Claude Usage Diff Monitor.

Single-machine design:
- official account usage comes from Claude Code's own ~/.claude.json usage
  cache (account-wide, refreshed periodically regardless of which session
  is active), falling back to the statusLine JSON's rate_limits block
- local project usage comes from Claude Code transcript JSONL
- no API key, no prompt text collection, no friend-side installation

Python 3.9+, standard library only.
"""

import json
import os
import platform
import sys
import time
from pathlib import Path
from collections import defaultdict

WINDOW_MINUTES = int(os.environ.get("CLAUDE_USAGE_WINDOW_MINUTES", "10"))
STALE_SECONDS = int(os.environ.get("CLAUDE_USAGE_STALE_SECONDS", "180"))
STATE_DIR = Path(os.environ.get(
    "CLAUDE_USAGE_STATE_DIR",
    str(Path.home() / ".claude" / "usage-diff")
)).expanduser()
STATE_FILE = STATE_DIR / "official_history.jsonl"


def now():
    return time.time()


def read_stdin_json():
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def cached_utilization():
    """Account-wide usage snapshot Claude Code refreshes into ~/.claude.json
    every so often on its own, independent of which session/window is
    active. The per-session statusLine stdin payload only refreshes its
    rate_limits block when THAT session gets a new API response, so an idle
    session freezes forever — this doesn't. Undocumented internal format,
    so any read failure just falls back to the stdin data below."""
    try:
        d = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return (d.get("cachedUsageUtilization") or {}).get("utilization") or {}
    except Exception:
        return {}


def rate_obj(data, cached, name):
    c = (cached or {}).get(name)
    if c and c.get("utilization") is not None:
        return {"used_percentage": c.get("utilization"), "resets_at": _iso_to_epoch(c.get("resets_at"))}
    return ((data.get("rate_limits") or {}).get(name) or {})


def pct(rate):
    try:
        return float(rate.get("used_percentage"))
    except (TypeError, ValueError):
        return None


def fmt_pct(v):
    return "--" if v is None else f"{v:.1f}%"


def fmt_tokens(v):
    if v is None:
        return "--"
    v = float(v)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}{v/1_000_000:.2f}M"
    if v >= 1000:
        return f"{sign}{v/1000:.1f}k"
    return f"{sign}{v:.0f}"


def fmt_rate(v):
    if v is None:
        return "--"
    return f"{fmt_tokens(v)}/min"


def fmt_ppm(v):
    if v is None:
        return "--"
    return f"{v:+.2f} pp/min"


def countdown(epoch):
    if not epoch:
        return "--"
    try:
        sec = max(0, int(float(epoch) - now()))
    except Exception:
        return "--"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def _last_snapshot():
    if not STATE_FILE.exists():
        return None
    try:
        lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def save_official(data):
    cached = cached_utilization()
    five = rate_obj(data, cached, "five_hour")
    seven = rate_obj(data, cached, "seven_day")

    snapshot = {
        "timestamp": now(),
        "five_pct": pct(five),
        "five_reset": five.get("resets_at"),
        "seven_pct": pct(seven),
        "seven_reset": seven.get("resets_at"),
        "session_id": data.get("session_id"),
        "project_dir": (data.get("workspace") or {}).get("project_dir"),
        "model": (data.get("model") or {}).get("display_name"),
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # An idle session keeps re-reporting the same cached rate_limits every
    # tick (Claude Code only refreshes them when that session gets a new API
    # response). Writing every 5s wastes the 2000-line ring buffer on
    # duplicates and evicts real history. Skip the write when unchanged and
    # still within STALE_SECONDS; always write on an actual change.
    last = _last_snapshot()
    unchanged = last and all(
        last.get(k) == snapshot[k]
        for k in ("five_pct", "five_reset", "seven_pct", "seven_reset")
    )
    if unchanged and snapshot["timestamp"] - last.get("timestamp", 0) < STALE_SECONDS:
        return snapshot

    with STATE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot) + "\n")

    # Keep last ~2000 snapshots.
    try:
        lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2000:
            STATE_FILE.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    return snapshot


def read_history():
    if not STATE_FILE.exists():
        return []
    out = []
    try:
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines()[-2000:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out


def official_rate(history, key="five_pct"):
    if len(history) < 2:
        return None

    cutoff = now() - WINDOW_MINUTES * 60
    recent = [x for x in history if x.get("timestamp", 0) >= cutoff and x.get(key) is not None]
    if len(recent) < 2:
        return None

    a = recent[0]
    b = recent[-1]
    dt = b["timestamp"] - a["timestamp"]
    if dt <= 0:
        return None

    return (b[key] - a[key]) / (dt / 60.0)


def transcript_files():
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return []
    return list(root.glob("**/*.jsonl"))


def parse_usage_records(path, since):
    """
    Read only records modified recently enough to matter.

    Claude Code transcript assistant messages contain a message.usage block.
    We sum each unique assistant message ID once.
    """
    total = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
    }
    per_project = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0
    })

    try:
        if path.stat().st_mtime < since - 120:
            return total, per_project

        # Transcript files are append-only in normal operation. Reading the
        # whole file is simple and robust for a personal monitor.
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                if obj.get("type") != "assistant":
                    continue

                ts = obj.get("timestamp")
                if ts:
                    try:
                        # ISO timestamp; Python stdlib handles Z.
                        from datetime import datetime
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        event_time = dt.timestamp()
                        if event_time < since:
                            continue
                    except Exception:
                        # If timestamp cannot be parsed, don't discard it.
                        pass

                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue

                msg_id = msg.get("id") or obj.get("uuid") or ""
                # Deduplication is local to this file and uses a set below.
                # For a simple file scan we store seen IDs in a local set.
                # Reimplemented in the caller for performance.
                item = {
                    "id": msg_id,
                    "input": int(usage.get("input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0),
                    "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_create": int(usage.get("cache_creation_input_tokens") or 0),
                    "timestamp": ts,
                }
                # Store records in a side list via a private key.
                per_project["__records__"].setdefault("records", []).append(item)

    except (OSError, UnicodeError):
        pass

    return total, per_project


def local_usage(window_minutes):
    since = now() - window_minutes * 60
    project_records = defaultdict(list)

    for path in transcript_files():
        project_name = path.parent.name
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                seen = set()
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "assistant":
                        continue

                    ts = obj.get("timestamp")
                    event_time = None
                    if ts:
                        try:
                            from datetime import datetime
                            event_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                    if event_time is not None and event_time < since:
                        continue

                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue

                    uid = msg.get("id") or obj.get("uuid") or f"{path}:{line[:80]}"
                    if uid in seen:
                        continue
                    seen.add(uid)

                    project_records[project_name].append({
                        "input": int(usage.get("input_tokens") or 0),
                        "output": int(usage.get("output_tokens") or 0),
                        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                        "cache_create": int(usage.get("cache_creation_input_tokens") or 0),
                    })
        except (OSError, UnicodeError):
            continue

    minutes = max(window_minutes, 1)
    projects = {}
    grand = 0

    for project, records in project_records.items():
        inp = sum(x["input"] for x in records)
        out = sum(x["output"] for x in records)
        cr = sum(x["cache_read"] for x in records)
        cc = sum(x["cache_create"] for x in records)
        total = inp + out + cr + cc
        if total:
            projects[project] = {
                "input": inp,
                "output": out,
                "cache_read": cr,
                "cache_create": cc,
                "total": total,
                "rate": total / minutes,
            }
            grand += total

    return projects, grand / minutes


def main():
    data = read_stdin_json()
    snap = save_official(data)
    history = read_history()

    five_rate = official_rate(history, "five_pct")
    seven_rate = official_rate(history, "seven_pct")
    projects, local_rate = local_usage(WINDOW_MINUTES)

    # Heuristic:
    # official account usage is moving upward while local token activity is
    # essentially zero. This is deliberately a qualitative signal.
    recent_official = five_rate is not None and five_rate > 0.02
    local_quiet = local_rate < 100  # less than 100 combined tokens/min

    signal = ""
    if recent_official and local_quiet:
        signal = " | ⚠ other activity likely"
    elif recent_official and local_rate >= 100:
        signal = " | account + your activity"
    elif five_rate is not None and five_rate <= 0.02:
        signal = " | no recent account increase"

    parts = [
        f"5h {fmt_pct(snap['five_pct'])}",
        f"{fmt_ppm(five_rate)}",
        f"↻ {countdown(snap['five_reset'])}",
        f"you {fmt_rate(local_rate)}",
        signal.strip(),
    ]

    print(" | ".join(x for x in parts if x))

    # Compact per-project attribution, up to 3 busiest.
    for project, info in sorted(projects.items(), key=lambda kv: kv[1]["rate"], reverse=True)[:3]:
        print(f"  {project}: {fmt_rate(info['rate'])}")


if __name__ == "__main__":
    main()
