#!/usr/bin/env python3
"""
Claude Usage Diff Dashboard — chart view (local-only).

Serves http://127.0.0.1:8766  (bound to 127.0.0.1 only; do NOT expose publicly)

Charts are inline SVG generated locally — no internet / CDN required, and no
data ever leaves this machine.

Time window defaults to the past 5 hours, bucketed every 10 minutes.
Override with environment variables:
  CLAUDE_USAGE_DIFF_PORT         (default 8766)
  CLAUDE_USAGE_DIFF_WINDOW_HOURS (default 5)
  CLAUDE_USAGE_DIFF_BUCKET_MIN   (default 10)
"""
import html as htmlmod
import json
import os
import time
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get("CLAUDE_USAGE_DIFF_PORT", "8766"))
STATE_DIR = Path(os.environ.get(
    "CLAUDE_USAGE_STATE_DIR",
    str(Path.home() / ".claude" / "usage-diff")
)).expanduser()
STATE_FILE = STATE_DIR / "official_history.jsonl"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"
WINDOW_HOURS = int(os.environ.get("CLAUDE_USAGE_DIFF_WINDOW_HOURS", "5"))
BUCKET_MIN = int(os.environ.get("CLAUDE_USAGE_DIFF_BUCKET_MIN", "10"))
MAX_BARS = 20  # limit conversation bar chart to top N
OFFICIAL_BUCKET_MIN = 5  # matches Ns_=300000ms, the write-debounce floor on
                         # Claude Code's own usage cache (see claude_usage_diff.py)
# Tokens-per-percentage-point ratio, for chart②'s right axis. From
# 2026-08-12 17:30-18:39 (single-user, per user confirmation): 27,651,524
# tokens moved official usage 5%->59% (54pp) = 512,065 tok/pp.
#
# This is NOT a stable constant — it's a rough period-average masking a
# large real split. The same 69-min window breaks into two very different
# sub-periods: 17:30-17:59 was 6.98M tokens for 45pp (155k tok/pp), while
# 17:59-18:39 was 20.67M tokens for just 9pp (2.30M tok/pp) — a 15x gap
# between two single-user sub-periods. That's too big to be noise or just
# the lumpy/delayed update batching (see claude_usage_diff.py) — it means
# raw token count is the wrong unit entirely. Official usage is almost
# certainly cost-weighted (model price + input/output/cache-read/
# cache-creation all priced very differently), so two periods with the same
# raw token count but a different type/model mix can move the % by very
# different amounts. A correct calibration would weight by approximate
# per-token-type cost, not sum tokens flatly like this does. Until that
# exists, treat this axis as a rough order-of-magnitude estimate only.
TOKENS_PER_PCT = 512065


def now():
    return time.time()


def load_history():
    if not STATE_FILE.exists():
        return []
    out = []
    try:
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out


def parse_iso(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _scan_transcript(p, since):
    """Return (ai_title, [(timestamp, tokens), ...]) for one transcript."""
    seen = set()
    title = None
    file_recs = []
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "ai-title":
                title = o.get("aiTitle") or title
                continue
            if o.get("type") != "assistant":
                continue
            iso = o.get("timestamp")
            t = parse_iso(iso) if iso else None
            m = o.get("message") or {}
            u = m.get("usage") or {}
            if not u:
                continue
            uid = m.get("id") or o.get("uuid") or f"{p.name}:{line[:60]}"
            if uid in seen:
                continue
            seen.add(uid)
            toks = (int(u.get("input_tokens") or 0)
                    + int(u.get("output_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0))
            if toks <= 0:
                continue
            if t is not None and t < since:
                continue
            file_recs.append((t if t is not None else p.stat().st_mtime, toks))
    return title, file_recs


def local_records(since):
    """Return (timestamp, tokens, label) for assistant usage messages in
    window. label identifies one conversation (project tag + Claude Code's
    own ai-title for that session), not just the project folder — a folder
    can hold many conversations and they'd otherwise be lumped together.

    Subagent transcripts (under <session-id>/subagents/*.jsonl) have no
    ai-title of their own, so they're attributed to their parent session's
    title instead — otherwise every subagent run across every conversation
    collapses into one opaque "subagents" bucket."""
    recs = []
    if not PROJECTS_ROOT.exists():
        return recs
    files = list(PROJECTS_ROOT.glob("**/*.jsonl"))
    top_level = [p for p in files if p.parent.name != "subagents"]
    subagents = [p for p in files if p.parent.name == "subagents"]

    titles = {}
    for p in top_level:
        try:
            title, file_recs = _scan_transcript(p, since)
        except Exception:
            continue
        titles[p.stem] = title
        if file_recs:
            label = f"{p.parent.name[-10:]}: {title or p.stem[:8]}"
            recs.extend((t, toks, label) for t, toks in file_recs)

    for p in subagents:
        try:
            _, file_recs = _scan_transcript(p, since)
        except Exception:
            continue
        if not file_recs:
            continue
        parent_id = p.parent.parent.name
        project_tag = p.parent.parent.parent.name[-10:]
        label = f"{project_tag}: {titles.get(parent_id) or parent_id[:8]} · 子代理"
        recs.extend((t, toks, label) for t, toks in file_recs)

    return recs


def official_series(history, since):
    pts = [(s.get("timestamp"), s.get("five_pct"))
           for s in history
           if s.get("timestamp") and s.get("five_pct") is not None
           and s["timestamp"] >= since]
    pts.sort(key=lambda x: x[0])
    return pts


def bucket_counts(since, recs, nbuckets):
    buckets = [0.0] * nbuckets
    for t, toks, _ in recs:
        idx = int((t - since) // (BUCKET_MIN * 60))
        if 0 <= idx < nbuckets:
            buckets[idx] += toks
    return buckets


def bucket_step_values(since, points, nbuckets, bucket_min):
    """Forward-fill sorted (timestamp, value) points into nbuckets of
    bucket_min minutes. five_pct is a gauge/level, not additive, so each
    bucket takes the last known value as of its end — not a sum. Buckets
    before the first point are None (no snapshot yet)."""
    out = []
    idx, n, last_val = 0, len(points), None
    for i in range(nbuckets):
        bucket_end = since + (i + 1) * bucket_min * 60
        while idx < n and points[idx][0] <= bucket_end:
            last_val = points[idx][1]
            idx += 1
        out.append(last_val)
    return out


# ---------------- SVG helpers ----------------

def yfmt_num(v):
    v = float(v)
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1000:
        return f"{v/1000:.1f}k"
    return f"{v:.0f}"


def esc(s):
    return htmlmod.escape(s, quote=True)


def line_chart(points, color, yfmt=lambda v: f"{v:,.0f}", empty="暂无数据",
               w=780, h=240, points2=None, color2=None, yfmt2=None):
    """points2 (optional): a second series sharing the same x-axis but its
    own independent y-scale, drawn dashed with right-side axis labels in
    color2 — for overlaying series with different units/magnitudes."""
    if len(points) < 1:
        return f'<p class="muted">{empty}</p>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    if maxx <= minx:
        maxx = minx + 1
    if maxy <= miny:
        maxy = miny + 1
    pad_l, pad_r, pad_t, pad_b = 54, (54 if points2 else 18), 16, 30
    iw, ih = w, h

    def sx(x):
        return pad_l + (x - minx) / (maxx - minx) * (iw - pad_l - pad_r)

    def sy(y):
        return ih - pad_b - (y - miny) / (maxy - miny) * (ih - pad_t - pad_b)

    svg = []
    yticks = 5
    for i in range(yticks + 1):
        y = miny + (maxy - miny) * i / yticks
        yy = sy(y)
        svg.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{iw-pad_r}" y2="{yy:.1f}" stroke="#ececec"/>')
        svg.append(f'<text x="{pad_l-8}" y="{yy+3:.1f}" font-size="10" fill="#888" text-anchor="end">{yfmt(y)}</text>')

    if points2:
        ys2 = [p[1] for p in points2]
        miny2, maxy2 = min(ys2), max(ys2)
        if maxy2 <= miny2:
            maxy2 = miny2 + 1

        def sy2(y):
            return ih - pad_b - (y - miny2) / (maxy2 - miny2) * (ih - pad_t - pad_b)

        for i in range(yticks + 1):
            y2 = miny2 + (maxy2 - miny2) * i / yticks
            svg.append(f'<text x="{iw-pad_r+8:.1f}" y="{sy2(y2)+3:.1f}" font-size="10" fill="{color2}" text-anchor="start">{yfmt2(y2)}</text>')

        if len(points2) == 1:
            x2, y2 = points2[0]
            svg.append(f'<circle cx="{sx(x2):.1f}" cy="{sy2(y2):.1f}" r="4" fill="{color2}"/>')
        else:
            pts2 = " ".join(f"{sx(x):.1f},{sy2(y):.1f}" for x, y in points2)
            svg.append(f'<polyline points="{pts2}" fill="none" stroke="{color2}" stroke-width="2" stroke-dasharray="5,3"/>')

    if len(points) == 1:
        x, y = points[0]
        svg.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="{color}"/>')
    else:
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
        svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        fill_pts = (f"{sx(points[0][0]):.1f},{ih-pad_b} "
                    + " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
                    + f" {sx(points[-1][0]):.1f},{ih-pad_b}")
        svg.append(f'<polygon points="{fill_pts}" fill="{color}" opacity="0.12"/>')

    n = min(6, len(points))
    for i in range(n):
        idx = round(i * (len(points) - 1) / (n - 1)) if n > 1 else 0
        x, _ = points[idx]
        label = time.strftime("%H:%M", time.localtime(x))
        svg.append(f'<text x="{sx(x):.1f}" y="{ih-pad_b+16}" font-size="10" fill="#888" text-anchor="middle">{label}</text>')

    return f'<svg viewBox="0 0 {iw} {ih}" style="width:100%;height:auto" role="img">{ "".join(svg) }</svg>'


def vbar_chart(values, since, bucket_min, color, yfmt=lambda v: f"{v:,.0f}",
               empty="暂无数据", w=780, h=240):
    """values: one per bucket, None = no snapshot yet for that bucket
    (skipped, not drawn as zero). Bars always start at 0 — unlike
    line_chart, a non-zero baseline would misrepresent bar height."""
    known = [v for v in values if v is not None]
    if not known:
        return f'<p class="muted">{empty}</p>'
    nbuckets = len(values)
    miny, maxy = 0.0, max(known)
    if maxy <= miny:
        maxy = miny + 1
    pad_l, pad_r, pad_t, pad_b = 54, 18, 16, 34
    iw, ih = w, h
    plot_w = iw - pad_l - pad_r
    bar_w = plot_w / nbuckets

    def sy(y):
        return ih - pad_b - (y - miny) / (maxy - miny) * (ih - pad_t - pad_b)

    svg = []
    yticks = 5
    for i in range(yticks + 1):
        y = miny + (maxy - miny) * i / yticks
        yy = sy(y)
        svg.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{iw-pad_r}" y2="{yy:.1f}" stroke="#ececec"/>')
        svg.append(f'<text x="{pad_l-8}" y="{yy+3:.1f}" font-size="10" fill="#888" text-anchor="end">{yfmt(y)}</text>')

    base_y = sy(miny)
    for i, v in enumerate(values):
        if v is None:
            continue
        x0 = pad_l + i * bar_w
        yy = sy(v)
        bw = max(1.0, bar_w - 1.5)
        svg.append(f'<rect x="{x0:.1f}" y="{min(yy, base_y):.1f}" width="{bw:.1f}" '
                    f'height="{max(0.5, abs(base_y - yy)):.1f}" fill="{color}" opacity="0.85"/>')

    n_labels = min(6, nbuckets)
    for k in range(n_labels):
        i = round(k * (nbuckets - 1) / (n_labels - 1)) if n_labels > 1 else 0
        t0 = since + i * bucket_min * 60
        t1 = t0 + bucket_min * 60
        rng = f"{time.strftime('%H:%M', time.localtime(t0))}-{time.strftime('%H:%M', time.localtime(t1))}"
        xc = pad_l + (i + 0.5) * bar_w
        svg.append(f'<text x="{xc:.1f}" y="{ih-pad_b+16}" font-size="9" fill="#888" text-anchor="middle">{rng}</text>')

    return f'<svg viewBox="0 0 {iw} {ih}" style="width:100%;height:auto" role="img">{ "".join(svg) }</svg>'


def bar_chart(items):
    """items: [(name, (prev_tokens, curr_tokens)), ...] — prev = usage
    before the current official 5h window's start, curr = at/after it.
    Rendered as a two-color split fill so a conversation spanning a reset
    boundary reads as one bar (part blue, part orange), not two bars."""
    if not items:
        return '<p class="muted">最近窗口内没有本地 token 活动</p>'
    items = items[:MAX_BARS]
    totals = [prev + curr for _, (prev, curr) in items]
    max_total = max(totals) if totals else 0
    rows = []
    for (name, (prev, curr)), total in zip(items, totals):
        full_pct = (total / max_total) * 100.0 if max_total > 0 else 0.0
        prev_frac = (prev / total) if total > 0 else 0.0
        w_prev = full_pct * prev_frac
        w_curr = full_pct - w_prev
        rows.append(
            f'<div class="brow"><div class="btrack">'
            f'<span class="bfill bfill-prev" style="left:0%;width:{w_prev:.2f}%"></span>'
            f'<span class="bfill bfill-curr" style="left:{w_prev:.2f}%;width:{w_curr:.2f}%"></span>'
            f'<span class="bname" title="{esc(name)}">{esc(name)}</span>'
            f'<span class="bval">{yfmt_num(total)}</span>'
            f'</div></div>'
        )
    return '<div class="barchart">' + "".join(rows) + '</div>'


def countdown(epoch):
    if not epoch:
        return "--"
    sec = max(0, int(float(epoch) - now()))
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


def html():
    nowt = now()
    since = nowt - WINDOW_HOURS * 3600
    nbuckets = max(1, int(WINDOW_HOURS * 60 / BUCKET_MIN))
    off_nbuckets = max(1, int(WINDOW_HOURS * 60 / OFFICIAL_BUCKET_MIN))

    hist = load_history()
    pts_official = official_series(hist, since)

    if len(pts_official) >= 2:
        dt_min = (pts_official[-1][0] - pts_official[0][0]) / 60.0
        rate = (pts_official[-1][1] - pts_official[0][1]) / dt_min if dt_min > 0 else None
    else:
        rate = None

    latest = hist[-1] if hist else {}
    five_pct = latest.get("five_pct")
    five_reset = latest.get("five_reset")
    # If the reset time has already passed but we're still showing the
    # pre-reset snapshot, the recording session went idle and Claude Code
    # never gave it a fresh rate_limits block to relay (it only refreshes
    # on that session's own API activity, not on a timer).
    stale = bool(five_reset) and five_reset < nowt
    # Start of the CURRENT official 5h window — used to split conversation
    # bars (③) and to reset the cumulative-usage series (②) at the same
    # boundary Claude Code itself uses, not the dashboard's own rolling
    # WINDOW_HOURS. None when we have no reset time to anchor to yet.
    window_boundary = None if stale or not five_reset else five_reset - 5 * 3600

    fetch_since = min(since, window_boundary) if window_boundary is not None else since
    recs = local_records(fetch_since)
    buckets = bucket_counts(since, recs, nbuckets)
    local_total = sum(buckets)
    local_ppm = local_total / max(1, WINDOW_HOURS * 60)

    convo_split = {}
    for t, toks, name in recs:
        if t < since:
            continue
        slot = convo_split.setdefault(name, [0, 0])
        if window_boundary is not None and t < window_boundary:
            slot[0] += toks
        else:
            slot[1] += toks
    convo_sorted = sorted(convo_split.items(), key=lambda kv: sum(kv[1]), reverse=True)

    # Cumulative local tokens since the current official window started,
    # converted to % via TOKENS_PER_PCT — right-axis series for chart②.
    # Buckets entirely before window_boundary must NOT contribute (that
    # usage belongs to the previous official window) — a separate sum from
    # `buckets` above, which intentionally includes everything in the
    # display range regardless of window boundary.
    carry_in = 0
    if window_boundary is not None and window_boundary < since:
        carry_in = sum(toks for t, toks, _ in recs if window_boundary <= t < since)
    cum_recs = recs if window_boundary is None else [r for r in recs if r[0] >= window_boundary]
    cum_buckets = bucket_counts(since, cum_recs, nbuckets)
    cum_tokens = []
    running = carry_in
    for b in cum_buckets:
        running += b
        cum_tokens.append(running)

    five_pct_disp = "--" if five_pct is None else f"{five_pct:.1f}%"
    rate_disp = "--" if rate is None else f"{rate:+.2f} pp/min"
    reset_disp = countdown(five_reset)
    local_disp = yfmt_num(local_total)

    signal, signal_cls = "暂无足够数据", "neutral"
    if stale:
        signal, signal_cls = (
            "⚠ 官方数据已停滞：记录数据的那个 Claude Code 会话已空闲太久（5h 重置时间已过但数值没变）—— "
            "官方用量只在你实际发消息的会话里才会刷新，不是按时间自动轮询。开一条新对话或在该窗口发条消息即可恢复更新。",
            "warn")
    elif rate is not None:
        if rate > 0.02 and local_ppm < 100:
            signal, signal_cls = ("⚠ 官方用量在上升，但你的本地活动很低 —— 疑似有其他会话在使用共享账号", "warn")
        elif rate > 0.02:
            signal, signal_cls = ("官方用量在上升，你的本地活动可以解释一部分", "ok")
        else:
            signal, signal_cls = ("官方用量近期无明显增长", "neutral")

    off_step_vals = bucket_step_values(since, pts_official, off_nbuckets, OFFICIAL_BUCKET_MIN)
    off_chart = vbar_chart(
        off_step_vals, since, OFFICIAL_BUCKET_MIN, "#2563eb", yfmt=lambda v: f"{v:.1f}%",
        empty="暂无官方快照（使用 Claude Code 时 statusLine 会陆续记录）")
    bucket_pts = [(since + i * BUCKET_MIN * 60, buckets[i]) for i in range(nbuckets)]
    cum_pts = [(since + i * BUCKET_MIN * 60, cum_tokens[i] / TOKENS_PER_PCT) for i in range(nbuckets)]
    loc_chart = line_chart(
        bucket_pts, "#10b981", yfmt=yfmt_num, empty="暂无本地 token 活动",
        points2=cum_pts, color2="#9333ea", yfmt2=lambda v: f"{v:.1f}%")
    convo_chart = bar_chart(convo_sorted)

    return f"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Usage Diff — 图表</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:980px;margin:32px auto;padding:0 20px;background:#f6f6f7;color:#222}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:#777;font-size:13px;margin-bottom:18px}}
.card{{background:#fff;border-radius:14px;padding:18px 20px;margin:14px 0;box-shadow:0 2px 10px rgba(0,0,0,.05)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.metric .k{{color:#888;font-size:12px}}
.metric .v{{font-size:23px;font-weight:700;margin-top:4px}}
.signal{{padding:14px 16px;border-radius:12px;font-size:15px}}
.signal.warn{{background:#fff4e5;color:#9a3412}}
.signal.ok{{background:#e7f6ec;color:#166534}}
.signal.neutral{{background:#f0f0f0;color:#555}}
.card h2{{font-size:15px;margin:0 0 10px}}
.muted{{color:#888;font-size:13px}}
.note{{color:#888;font-size:12px;line-height:1.7;margin-top:8px}}
.barchart{{margin-top:4px}}
.brow{{margin:8px 0}}
.btrack{{position:relative;height:26px;background:#f1f1f1;border-radius:8px;overflow:hidden}}
.bfill{{position:absolute;top:0;bottom:0}}
.bfill-prev{{background:#2563eb}}
.bfill-curr{{background:#f59e0b}}
.bname{{position:absolute;left:12px;top:0;bottom:0;display:flex;align-items:center;font-size:12.5px;color:#fff;font-weight:600;text-shadow:0 1px 2px rgba(0,0,0,.35);white-space:nowrap;z-index:1}}
.bval{{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:12px;font-weight:700;color:#333;background:rgba(255,255,255,.92);border-radius:5px;padding:1px 8px;z-index:2}}
svg text{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
</style>
</head><body>
<h1>Claude Usage Diff</h1>
<div class="sub">官方账号用量 vs 你的本地 token 活动 · 时间窗口：过去 {WINDOW_HOURS} 小时 · 每 {BUCKET_MIN} 分钟一个点 · 页面每 10 秒自动刷新</div>

<div class="grid">
  <div class="card metric"><div class="k">官方 5h 用量（最新）</div><div class="v">{five_pct_disp}</div></div>
  <div class="card metric"><div class="k">官方 5h 变化率（窗口）</div><div class="v">{rate_disp}</div></div>
  <div class="card metric"><div class="k">5h 重置倒计时</div><div class="v">{reset_disp}</div></div>
  <div class="card metric"><div class="k">本地 token（{WINDOW_HOURS}h 总量）</div><div class="v">{local_disp}</div></div>
</div>

<div class="card"><div class="signal {signal_cls}">{signal}</div></div>

<div class="card"><h2>① 官方 5h 用量 %（每 {OFFICIAL_BUCKET_MIN} 分钟一根柱，过去 {WINDOW_HOURS} 小时）</h2>{off_chart}<div class="note">蓝色柱 = 每 {OFFICIAL_BUCKET_MIN} 分钟区间末尾时刻的官方 5 小时用量百分比（阶梯取值，非求和）。{OFFICIAL_BUCKET_MIN} 分钟对应 Claude Code 自己刷新用量缓存的最快频率（Ns_=300000ms），更密也测不出新数据。</div></div>

<div class="card"><h2>② 你的本地 token 用量（每 {BUCKET_MIN} 分钟，过去 {WINDOW_HOURS} 小时）</h2>{loc_chart}<div class="note">绿色（左轴）= 每 {BUCKET_MIN} 分钟消耗的 token 数（速率）。紫色虚线（右轴，%）= 从当前官方 5 小时窗口起点累计的 token 用量，按 TOKENS_PER_PCT={TOKENS_PER_PCT:,} 折算成百分比 —— <strong>系数来自 2026-08-12 17:30-18:39 单人使用数据，但同一时段内拆成两半会差 15 倍（155k vs 2.30M tok/pp），说明官方用量大概率是按模型和 token 类型加权计费的，不是简单数 token；这个右轴只能当量级参考，不是精确值</strong>。</div></div>

<div class="card"><h2>③ 各对话占用 token（过去 {WINDOW_HOURS} 小时）</h2>{convo_chart}<div class="note">标签 = 项目名末 10 字符 + 对话标题，只列前 {MAX_BARS} 个。颜色按当前官方 5 小时窗口拆分：蓝色 = 该窗口开始前的用量，橙色 = 窗口开始后的用量；横跨窗口边界的对话会同时显示两色。</div></div>

<div class="card note">单位说明：官方百分比（percentage points）和 token 是不同单位，两条曲线<strong>不能直接换算</strong>。本页只是按时间对齐展示两者的走势，用来判断：官方用量在增长时，能不能用你自己的本地活动来解释。若①在涨、②却很平，就提示可能有其他会话在用共享账号。</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        b = html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Claude Usage Diff dashboard: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
