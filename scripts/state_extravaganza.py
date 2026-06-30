#!/usr/bin/env python3
"""
State Extravaganza: a manual, on-demand digest thread that spotlights recent
bill activity from a HAND-PICKED set of states (rather than the whole country),
posted to a single platform.

Where the weekly digest is topic-first ("everything moving on <topic> across all
states this week"), the State Extravaganza is state-first: pick one or more
states, pick how far back to look (never more than 62 days), and ship a thread
of the most significant bills those statehouses produced. The first post is a
formal "🏛️ State Extravaganza!! 🧵" header; each reply is one bill card, chained
into a single thread exactly like the weekly digest.

Configurable knobs (all via env vars; the manual workflow wires them to the
"Run workflow" form):

  * PLATFORM                 — bluesky | x | threads  (which feed to post to)
  * EXTRAVAGANZA_STATES      — space/comma separated state codes, e.g. "CA NY TX"
                               (empty = every state, i.e. a national extravaganza)
  * BOT_TOPIC                — topic folder; selects the bill filter, the copy
                               focus, and (for Bluesky/Threads) the account
                               credentials. X is a single account, so the
                               workflow pins BOT_TOPIC to the X bot's topic.
  * NUM_POSTS                — number of bill posts in the thread (max highlights)
  * EXTRAVAGANZA_LOOKBACK_DAYS — recency window in days; HARD-CAPPED at 62.
  * EXTRAVAGANZA_PER_STATE_CAP — max bills per state (defaults to NUM_POSTS, i.e.
                               effectively uncapped so a single-state run can
                               fill the whole thread).
  * DRY_RUN                  — "1" composes + prints the thread without posting.

Reuse, not duplication: the significance scorer, the bill loader/filter, every
platform's post composition, and the thread-chaining all come from the existing
weekly-digest + daily-poster modules. The only logic unique to this file is the
state filter, the 62-day cap, and the extravaganza root copy.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone

from post_to_bluesky import (
    JSONL_PATH,
    STATE_FULL_NAME,
    TOPIC,
    US_STATES,
    load_bills,
)
from weekly_digest_bluesky import (
    _format_short,
    candidates_in_window,
    collect_topic_bills,
    score_action,
)

# The whole point of this digest is a recent-activity spotlight, so the lookback
# window is never allowed past 62 days no matter what the workflow form passes.
MAX_LOOKBACK_DAYS = 62

PLATFORM = os.environ.get("PLATFORM", "bluesky").strip().lower()
NUM_POSTS = max(1, int(os.environ.get("NUM_POSTS", "6")))
# Per-state cap defaults to NUM_POSTS — i.e. no real cap — so a one-state
# extravaganza can fill every slot from that state. Set EXTRAVAGANZA_PER_STATE_CAP
# to a smaller number to keep a multi-state run broad.
PER_STATE_CAP = max(1, int(os.environ.get("EXTRAVAGANZA_PER_STATE_CAP", str(NUM_POSTS))))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def _resolve_lookback() -> int:
    raw = os.environ.get("EXTRAVAGANZA_LOOKBACK_DAYS", str(MAX_LOOKBACK_DAYS))
    try:
        days = int(raw)
    except ValueError:
        days = MAX_LOOKBACK_DAYS
    if days < 1:
        days = 1
    if days > MAX_LOOKBACK_DAYS:
        print(f"  lookback {days}d exceeds the {MAX_LOOKBACK_DAYS}-day cap; "
              f"clamping to {MAX_LOOKBACK_DAYS}.")
        days = MAX_LOOKBACK_DAYS
    return days


def parse_states() -> list[str]:
    """Parse EXTRAVAGANZA_STATES into a list of valid, de-duplicated, uppercase
    state codes (order preserved). Empty/unset means "all states"."""
    raw = os.environ.get("EXTRAVAGANZA_STATES", "")
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw.replace(",", " ").split():
        code = tok.strip().upper()
        if not code or code in seen:
            continue
        if code not in US_STATES:
            print(f"  ! ignoring unknown state code: {tok!r}", file=sys.stderr)
            continue
        seen.add(code)
        out.append(code)
    return out


def filter_by_states(bills: list[dict], states: list[str]) -> list[dict]:
    if not states:
        return bills
    want = set(states)
    return [b for b in bills if (b.get("state") or "").upper() in want]


def states_label(states: list[str]) -> str:
    """Human-readable scope line for the root post. Spells out full state names
    for a handful of states, collapses to a count for a big list."""
    if not states:
        return "all states"
    names = [STATE_FULL_NAME.get(c, c) for c in states]
    if len(names) == 1:
        return names[0]
    if len(names) <= 4:
        return ", ".join(names[:-1]) + " & " + names[-1]
    return f"{len(names)} states"


def select_extravaganza(candidates: list[dict], max_posts: int,
                        per_state_cap: int) -> list[dict]:
    """Collapse each bill to its highest-scoring action, then take the top
    `max_posts` by (significance score, recency), capped at `per_state_cap`
    bills per state. Mirrors weekly_digest_bluesky.select_highlights but reads
    the extravaganza's own NUM_POSTS / per-state knobs instead of the digest
    globals."""
    best_by_bill: dict[tuple[str, str], dict] = {}
    for b in candidates:
        key = (b["state"], b["identifier"])
        b["_score"] = score_action(b["action_desc"])
        existing = best_by_bill.get(key)
        if existing is None or b["_score"] > existing["_score"] or (
            b["_score"] == existing["_score"]
            and b["action_date"] > existing["action_date"]
        ):
            best_by_bill[key] = b

    bills = sorted(best_by_bill.values(),
                   key=lambda b: (b["_score"], b["action_date"]), reverse=True)

    picked: list[dict] = []
    per_state: Counter[str] = Counter()
    for b in bills:
        state = b["state"] or "?"
        if per_state[state] >= per_state_cap:
            continue
        picked.append(b)
        per_state[state] += 1
        if len(picked) >= max_posts:
            break
    return picked


def compose_root(scope: str, topic_label: str, today: datetime,
                 window_days: int, max_len: int, len_fn, *,
                 has_links: bool = False) -> str:
    """Build the formal extravaganza header post, trimming progressively to fit
    the platform's character budget (len_fn measures it — len for Bluesky/
    Threads, x_weighted_len for X)."""
    range_str = f"past {window_days} days"
    title = "🏛️ State Extravaganza!! 🧵"
    framing = (f"Spotlighting {topic_label} bill activity from {scope} "
               f"over the {range_str}.")
    links_line = "\n🔗 All bill links are in the last post." if has_links else ""
    text = f"{title}\n{scope}\n\n{framing}{links_line}"
    if len_fn(text) > max_len:
        text = f"{title}\n\n{framing}{links_line}"
    if len_fn(text) > max_len:
        text = f"{title}\n{scope}{links_line}"
    if len_fn(text) > max_len:
        text = f"{title}{links_line}"
    return text


# ---------------------------------------------------------------------------
# Per-platform handlers
# ---------------------------------------------------------------------------

def run_bluesky(candidates: list[dict], scope: str, today: datetime,
                window: int) -> int:
    from post_to_bluesky import BSKY_HANDLE, BSKY_PASSWORD, BlueskyClient, MAX_POST
    from weekly_digest_bluesky import (
        _build_highlight_replies,
        _save_digest_raw_records,
        post_thread,
    )

    if not DRY_RUN and (not BSKY_HANDLE or not BSKY_PASSWORD):
        print("ERROR: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must be set "
              "for this topic.", file=sys.stderr)
        return 1

    highlights = select_extravaganza(candidates, NUM_POSTS, PER_STATE_CAP)
    _log_highlights(highlights, window)

    client = None if DRY_RUN else BlueskyClient(BSKY_HANDLE, BSKY_PASSWORD)
    _save_digest_raw_records(highlights)
    replies = _build_highlight_replies(client, highlights)
    root_text = compose_root(scope, TOPIC.display_name, today, window,
                             MAX_POST, len)
    post_thread(client, root_text, replies)
    print(f"\nDone. Posted Bluesky State Extravaganza: 1 root + "
          f"{len(replies)} bill post(s).")
    return 0


def run_x(candidates: list[dict], scope: str, today: datetime,
          window: int) -> int:
    from post_to_x import (
        MAX_TWEET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET,
        X_API_KEY,
        X_API_SECRET,
        build_client,
        x_weighted_len,
    )
    from weekly_digest_x import (
        _save_digest_raw_records,
        build_highlight_replies,
        build_link_posts,
        post_thread,
    )

    missing = [n for n, v in (
        ("X_API_KEY", X_API_KEY),
        ("X_API_SECRET", X_API_SECRET),
        ("X_ACCESS_TOKEN", X_ACCESS_TOKEN),
        ("X_ACCESS_TOKEN_SECRET", X_ACCESS_TOKEN_SECRET),
    ) if not v]
    if missing and not DRY_RUN:
        print(f"ERROR: missing X credentials: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    highlights = select_extravaganza(candidates, NUM_POSTS, PER_STATE_CAP)
    _log_highlights(highlights, window)

    client = None if DRY_RUN else build_client()
    _save_digest_raw_records(highlights)
    replies, link_items = build_highlight_replies(highlights)
    link_posts = build_link_posts(link_items)
    root_text = compose_root(scope, TOPIC.display_name, today, window,
                             MAX_TWEET, x_weighted_len,
                             has_links=bool(link_posts))
    post_thread(client, root_text, replies + link_posts)
    print(f"\nDone. Posted X State Extravaganza: 1 root + {len(replies)} "
          f"bill post(s) + {len(link_posts)} links post(s).")
    return 0


def run_threads(candidates: list[dict], scope: str, today: datetime,
                window: int) -> int:
    from post_to_meta_threads import (
        MAX_THREADS,
        THREADS_ACCESS_TOKEN,
        THREADS_USER_ID,
    )
    from weekly_digest_meta_threads import (
        _save_digest_raw_records,
        build_highlight_replies,
        post_digest_thread,
    )

    missing = [n for n, v in (
        ("THREADS_ACCESS_TOKEN", THREADS_ACCESS_TOKEN),
        ("THREADS_USER_ID", THREADS_USER_ID),
    ) if not v]
    if missing and not DRY_RUN:
        print(f"ERROR: missing Threads credentials: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    highlights = select_extravaganza(candidates, NUM_POSTS, PER_STATE_CAP)
    _log_highlights(highlights, window)

    _save_digest_raw_records(highlights)
    replies = build_highlight_replies(highlights)
    root_text = compose_root(scope, TOPIC.display_name, today, window,
                             MAX_THREADS, len)
    post_digest_thread(root_text, replies)
    print(f"\nDone. Posted Threads State Extravaganza: 1 root + "
          f"{len(replies)} bill post(s).")
    return 0


_HANDLERS = {
    "bluesky": run_bluesky,
    "x": run_x,
    "threads": run_threads,
}


def _log_highlights(highlights: list[dict], window: int) -> None:
    print(f"\nSelected {len(highlights)} bill(s) (max={NUM_POSTS}, "
          f"per-state-cap={PER_STATE_CAP}, window={window}d):")
    for b in highlights:
        print(f"  [{b['_score']:>3}] {b['state']} {b['identifier']} "
              f"({b['action_date']}): {b['action_desc'][:70]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if PLATFORM not in _HANDLERS:
        print(f"ERROR: PLATFORM must be one of {', '.join(_HANDLERS)} "
              f"(got {PLATFORM!r}).", file=sys.stderr)
        return 1

    states = parse_states()
    window = _resolve_lookback()
    scope = states_label(states)
    print(f"=== State Extravaganza: platform={PLATFORM}, topic={TOPIC.name}, "
          f"states={scope}, window={window}d, posts={NUM_POSTS} ===")

    records = load_bills(JSONL_PATH)
    if not records:
        return 0

    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    all_bills = collect_topic_bills(records)
    if not all_bills:
        print(f"No {TOPIC.topic_phrase} bills found at all. Nothing to post.")
        return 0

    in_states = filter_by_states(all_bills, states)
    if not in_states:
        print(f"No {TOPIC.topic_phrase} bills found for {scope}. Nothing to post.")
        return 0

    candidates = candidates_in_window(in_states, today, window)
    print(f"Lookback {window}d: {len(candidates)} {TOPIC.topic_phrase} "
          f"bill update(s) from {scope}.")
    if not candidates:
        print(f"No {TOPIC.topic_phrase} activity for {scope} in the last "
              f"{window} days. Nothing to post.")
        return 0

    unique_bills = {(b["state"], b["identifier"]) for b in candidates}
    state_counts = Counter(s or "?" for s, _ in unique_bills)
    print(f"  unique bills: {len(unique_bills)} (from {len(candidates)} "
          f"action entries)")
    print(f"  by state: "
          f"{', '.join(f'{s}={n}' for s, n in state_counts.most_common(15))}")

    return _HANDLERS[PLATFORM](candidates, scope, today, window)


if __name__ == "__main__":
    sys.exit(main())
