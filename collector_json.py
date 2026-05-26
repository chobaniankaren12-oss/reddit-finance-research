"""Public Reddit JSON-endpoint collector — no Reddit API credentials needed.

Reddit exposes anonymous read-only listings at:
    https://www.reddit.com/r/<sub>/<listing>.json?limit=N

This module is a drop-in alternative to `collector.py` (PRAW) for the cases
where you don't yet have a Reddit script-app approved. It writes posts into
the same SQLite tables — classification + downstream stages don't care which
collector produced the data.

Comments are optional and slow (one extra request per post). Enable with
`--comments-per-post N` on the CLI.

Limitations vs. the PRAW collector:
    - No NSFW / quarantined / private subreddits.
    - Listing pagination is capped at ~1000 items by Reddit.
    - Comments tree depth doesn't expand `kind: more` placeholders (would
      need extra requests). You get whatever Reddit returns in one shot.
    - Per-account rate limit is harder without OAuth; we sleep 1.5s
      between requests to stay polite.

Usage:
    python cli.py collect-json --sub personalfinance --listing hot --limit 50
    python cli.py collect-json --seeds --listing top --time week --limit 100 --comments-per-post 30
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import IGNORE_AUTHORS, MIN_COMMENTS, MIN_UPVOTES
from storage import get_conn, json_dump, upsert_comment, upsert_post, upsert_subreddit

logger = logging.getLogger(__name__)


# Reddit insists on a descriptive User-Agent; without it you get 429s fast.
USER_AGENT = "reddit-finance-research/0.1 (personal-research educational use)"
RATE_DELAY_SEC = 1.5
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE_SEC = 4.0


def _fetch_json(url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """GET with exponential backoff on 429/5xx. Raises on persistent failure."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_exc = exc
            sleep_for = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.warning(
                "GET %s failed (%s) attempt %d/%d — sleeping %.1fs",
                url, exc, attempt, MAX_RETRIES, sleep_for,
            )
            time.sleep(sleep_for)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            sleep_for = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.warning(
                "GET %s returned %d — backing off %.1fs (attempt %d/%d)",
                url, resp.status_code, sleep_for, attempt, MAX_RETRIES,
            )
            time.sleep(sleep_for)
            continue
        # 403 (banned subs), 404 (deleted), etc. — non-retriable
        resp.raise_for_status()
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts ({last_exc})")


def _fetch_about(name: str) -> dict[str, Any]:
    """Subreddit metadata: subscribers, description."""
    payload = _fetch_json(f"https://www.reddit.com/r/{name}/about.json")
    return payload.get("data", {})


def _fetch_listing(
    name: str, listing: str, limit: int, time_filter: str | None
) -> list[dict[str, Any]]:
    """Listing fetcher. `listing` in {hot,new,top,rising}, time_filter for top."""
    url = f"https://www.reddit.com/r/{name}/{listing}.json"
    params = {"limit": str(limit), "raw_json": "1"}
    if listing == "top" and time_filter:
        params["t"] = time_filter
    payload = _fetch_json(url, params=params)
    return payload.get("data", {}).get("children", []) or []


def _fetch_comments(post_id_short: str, sub: str, limit: int) -> list[dict[str, Any]]:
    """Fetch top-N comments for one post via the comments JSON endpoint.

    Returns a flat list of comment data dicts in display order (top-sorted).
    `kind: more` placeholders and deeply nested replies past depth-5 are skipped.
    """
    url = f"https://www.reddit.com/r/{sub}/comments/{post_id_short}.json"
    params = {"limit": str(limit), "sort": "top", "raw_json": "1", "depth": "5"}
    payload = _fetch_json(url, params=params)
    # Response is a 2-element array: [post_listing, comments_listing]
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    comments_listing = payload[1].get("data", {}).get("children", []) or []
    out: list[dict[str, Any]] = []
    _walk_comments(comments_listing, out, depth=0, max_depth=5)
    return out[:limit]


def _walk_comments(
    nodes: list[dict[str, Any]],
    out: list[dict[str, Any]],
    depth: int,
    max_depth: int,
) -> None:
    """Recursively flatten the nested comments tree into a single list."""
    for node in nodes:
        kind = node.get("kind")
        if kind == "more":
            continue  # would need a second request to expand
        if kind != "t1":
            continue
        data = node.get("data") or {}
        data["_depth"] = depth
        out.append(data)
        replies = data.get("replies")
        # `replies` is either an empty string (no replies) or a Listing dict.
        if isinstance(replies, dict) and depth + 1 <= max_depth:
            kids = replies.get("data", {}).get("children", []) or []
            _walk_comments(kids, out, depth + 1, max_depth)


def _passes_filters(p: dict[str, Any]) -> bool:
    if p.get("author") in IGNORE_AUTHORS:
        return False
    if p.get("score", 0) < MIN_UPVOTES and p.get("num_comments", 0) < MIN_COMMENTS:
        return False
    return True


def _to_post_row(p: dict[str, Any], now_utc: int) -> dict[str, Any]:
    selftext = p.get("selftext") or ""
    is_deleted = selftext in {"[deleted]", "[removed]"} or p.get("author") in {
        "[deleted]",
        None,
    }
    upvotes = int(p.get("score") or 0)
    nc = int(p.get("num_comments") or 0)
    awards = int(p.get("total_awards_received") or 0)
    return {
        "id": p["name"],  # already in t3_xxx form
        "subreddit_name": str(p.get("subreddit", "")).lower(),
        "title": p.get("title") or "",
        "selftext": selftext,
        "url": f"https://reddit.com{p.get('permalink', '')}",
        "author": str(p.get("author") or "[deleted]"),
        "upvotes": upvotes,
        "downvote_ratio": float(p.get("upvote_ratio") or 0.0),
        "num_comments": nc,
        "awards": awards,
        "created_utc": int(p.get("created_utc") or 0),
        "fetched_at": now_utc,
        "flair": p.get("link_flair_text"),
        "is_sticky": int(bool(p.get("stickied"))),
        "is_locked": int(bool(p.get("locked"))),
        "is_deleted": int(is_deleted),
        "hotness_score": upvotes * 0.3 + nc * 0.5 + awards * 0.2,
        "engagement_ratio": nc / max(upvotes, 1),
        "raw_json": json_dump(
            {"id": p.get("id"), "permalink": p.get("permalink"), "via": "json"}
        ),
    }


def _to_comment_row(
    c: dict[str, Any], post_fullname: str, now_utc: int
) -> dict[str, Any]:
    body = c.get("body") or ""
    is_deleted = body in {"[deleted]", "[removed]"} or c.get("author") in {
        "[deleted]", None,
    }
    return {
        "id": c.get("name") or f"t1_{c.get('id', '')}",
        "post_id": post_fullname,
        "parent_id": c.get("parent_id"),
        "author": str(c.get("author") or "[deleted]"),
        "body": body,
        "upvotes": int(c.get("score") or 0),
        "created_utc": int(c.get("created_utc") or 0),
        "fetched_at": now_utc,
        "depth": int(c.get("_depth") or c.get("depth") or 0),
        "is_deleted": int(is_deleted),
        "raw_json": json_dump(
            {"id": c.get("id"), "parent_id": c.get("parent_id"), "via": "json"}
        ),
    }


def _comment_passes(c: dict[str, Any], min_score: int, min_length: int) -> bool:
    """Filter out low-signal comments before storage."""
    if c.get("author") in IGNORE_AUTHORS:
        return False
    if c.get("author") == "AutoModerator":
        return False
    body = c.get("body") or ""
    if body in {"[deleted]", "[removed]"}:
        return False
    if len(body) < min_length:
        return False
    if int(c.get("score") or 0) < min_score:
        return False
    return True


def collect_subreddit(
    name: str,
    listings: list[tuple[str, int, str | None]],
    comments_per_post: int = 0,
    min_comment_score: int = 1,
    min_comment_length: int = 50,
) -> dict[str, int]:
    """Fetch one subreddit across multiple listings, optionally with comments.

    `listings` is a list of (listing_name, limit, time_filter) tuples.
    `comments_per_post`: 0 = skip comments (default), >0 = fetch top-N per post.
    """
    totals = {"seen": 0, "kept": 0, "comments": 0, "listings": 0}
    now = int(time.time())

    # Subreddit /about
    try:
        about = _fetch_about(name)
        sub_row = {
            "id": about.get("name") or f"json_{name.lower()}",
            "name": (about.get("display_name") or name).lower(),
            "subscribers": int(about.get("subscribers") or 0),
            "description": about.get("public_description") or "",
            "fetched_at": now,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("r/%s /about failed (%s) — using stub", name, exc)
        sub_row = {
            "id": f"json_{name.lower()}",
            "name": name.lower(),
            "subscribers": 0,
            "description": "",
            "fetched_at": now,
        }

    with get_conn() as conn:
        upsert_subreddit(conn, sub_row)

    time.sleep(RATE_DELAY_SEC)

    for listing, limit, t_filter in listings:
        totals["listings"] += 1
        try:
            children = _fetch_listing(name, listing, limit, t_filter)
        except Exception as exc:  # noqa: BLE001
            logger.error("r/%s/%s failed: %s", name, listing, exc)
            time.sleep(RATE_DELAY_SEC)
            continue

        kept_here = 0
        kept_posts_data: list[tuple[str, str]] = []  # (post_fullname, post_id_short)
        with get_conn() as conn:
            for child in children:
                if child.get("kind") != "t3":
                    continue
                data = child.get("data") or {}
                totals["seen"] += 1
                if not _passes_filters(data):
                    continue
                try:
                    upsert_post(conn, _to_post_row(data, int(time.time())))
                    kept_here += 1
                    totals["kept"] += 1
                    kept_posts_data.append((data["name"], data["id"]))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "post upsert failed (%s): %s", data.get("id", "?"), exc
                    )
        logger.info(
            "r/%s/%s%s  seen=%d  kept=%d",
            name, listing,
            f" t={t_filter}" if t_filter else "",
            len(children), kept_here,
        )
        time.sleep(RATE_DELAY_SEC)

        # Optional second pass: fetch comments for each kept post
        if comments_per_post > 0 and kept_posts_data:
            for post_fullname, post_id_short in kept_posts_data:
                try:
                    comments = _fetch_comments(post_id_short, name, comments_per_post)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("r/%s comments for %s failed: %s",
                                   name, post_id_short, exc)
                    time.sleep(RATE_DELAY_SEC)
                    continue
                kept_c = 0
                with get_conn() as conn:
                    for c in comments:
                        if not _comment_passes(c, min_comment_score, min_comment_length):
                            continue
                        try:
                            upsert_comment(
                                conn,
                                _to_comment_row(c, post_fullname, int(time.time())),
                            )
                            kept_c += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("comment upsert failed: %s", exc)
                totals["comments"] += kept_c
                time.sleep(RATE_DELAY_SEC)
            logger.info(
                "r/%s  comments collected so far: %d", name, totals["comments"]
            )

    return totals


def collect_many(
    names: list[str],
    listings: list[tuple[str, int, str | None]],
    comments_per_post: int = 0,
    min_comment_score: int = 1,
    min_comment_length: int = 50,
) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    for name in names:
        try:
            results[name] = collect_subreddit(
                name,
                listings,
                comments_per_post=comments_per_post,
                min_comment_score=min_comment_score,
                min_comment_length=min_comment_length,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("r/%s failed entirely: %s", name, exc)
            results[name] = {"seen": 0, "kept": 0, "comments": 0, "listings": 0}
    return results
