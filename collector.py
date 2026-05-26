"""Reddit collection via PRAW.

Iterates over configured listings (hot / top_week / top_month / top_year / new / rising)
for each subreddit, persists posts + comments via storage.py, and tracks per-listing
metrics in collection_log.

Idempotent: repeated runs UPSERT by Reddit fullname.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import praw
import prawcore
from tqdm import tqdm

from config import (
    CHECKPOINT_EVERY,
    IGNORE_AUTHORS,
    LISTING_LIMITS,
    MAX_COMMENTS_PER_POST,
    MIN_COMMENTS,
    MIN_UPVOTES,
    MORE_COMMENTS_EXPANSION,
    RAW_DUMP,
    RAW_DIR,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
)
from storage import (
    bump_discovered,
    get_conn,
    json_dump,
    log_collection_finish,
    log_collection_start,
    upsert_comment,
    upsert_post,
    upsert_subreddit,
)

logger = logging.getLogger(__name__)


def make_reddit() -> praw.Reddit:
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        raise RuntimeError(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET missing. Fill .env first."
        )
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        check_for_async=False,
    )
    reddit.read_only = True
    return reddit


def _with_retry(fn, *args, **kwargs):
    """Call fn with exponential backoff on transient PRAW errors."""
    delay = RETRY_BASE_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except (
            prawcore.exceptions.ServerError,
            prawcore.exceptions.RequestException,
            prawcore.exceptions.ResponseException,
        ) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 429 / 5xx -> backoff; 403/404 -> give up immediately
            if status in {403, 404}:
                logger.warning("Non-retriable HTTP %s on %s — skipping", status, fn.__name__)
                raise
            logger.warning(
                "Transient error on attempt %d/%d (%s) — sleeping %.1fs",
                attempt, RETRY_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
            delay *= 2
        except prawcore.exceptions.OAuthException:
            raise
    assert last_exc is not None
    raise last_exc


# --- Field extraction --------------------------------------------------------


def _post_passes_filters(submission: praw.models.Submission) -> bool:
    author = str(submission.author) if submission.author else "[deleted]"
    if author in IGNORE_AUTHORS:
        return False
    # Generous: keep if EITHER threshold passes.
    if submission.score < MIN_UPVOTES and submission.num_comments < MIN_COMMENTS:
        return False
    return True


def _hotness(upvotes: int, num_comments: int, awards: int) -> float:
    return upvotes * 0.3 + num_comments * 0.5 + awards * 0.2


def _engagement(upvotes: int, num_comments: int) -> float:
    return num_comments / max(upvotes, 1)


def _post_row(submission: praw.models.Submission, now_utc: int) -> dict[str, Any]:
    selftext = submission.selftext or ""
    is_deleted = selftext in {"[deleted]", "[removed]"}
    author = str(submission.author) if submission.author else "[deleted]"
    awards = int(getattr(submission, "total_awards_received", 0) or 0)
    upvotes = int(submission.score or 0)
    num_comments = int(submission.num_comments or 0)
    raw = {
        "id": submission.id,
        "permalink": submission.permalink,
        "subreddit": str(submission.subreddit),
    } if not RAW_DUMP else _full_submission_raw(submission)
    return {
        "id": submission.fullname,                # e.g. t3_xxx
        "subreddit_name": str(submission.subreddit).lower(),
        "title": submission.title,
        "selftext": selftext,
        "url": f"https://reddit.com{submission.permalink}",
        "author": author,
        "upvotes": upvotes,
        "downvote_ratio": float(getattr(submission, "upvote_ratio", 0.0) or 0.0),
        "num_comments": num_comments,
        "awards": awards,
        "created_utc": int(submission.created_utc or 0),
        "fetched_at": now_utc,
        "flair": submission.link_flair_text,
        "is_sticky": int(bool(submission.stickied)),
        "is_locked": int(bool(submission.locked)),
        "is_deleted": int(is_deleted),
        "hotness_score": _hotness(upvotes, num_comments, awards),
        "engagement_ratio": _engagement(upvotes, num_comments),
        "raw_json": json_dump(raw),
    }


def _full_submission_raw(submission: praw.models.Submission) -> dict[str, Any]:
    # Re-extract via vars() but drop the live PRAW handle to keep JSON-safe.
    keys = (
        "id", "title", "selftext", "url", "permalink", "score", "upvote_ratio",
        "num_comments", "total_awards_received", "created_utc", "stickied",
        "locked", "link_flair_text", "over_18",
    )
    return {k: getattr(submission, k, None) for k in keys} | {"subreddit": str(submission.subreddit)}


def _comment_row(comment: praw.models.Comment, post_fullname: str, now_utc: int) -> dict[str, Any]:
    body = comment.body or ""
    is_deleted = body in {"[deleted]", "[removed]"}
    author = str(comment.author) if comment.author else "[deleted]"
    return {
        "id": comment.fullname,                   # t1_xxx
        "post_id": post_fullname,
        "parent_id": comment.parent_id,
        "author": author,
        "body": body,
        "upvotes": int(comment.score or 0),
        "created_utc": int(comment.created_utc or 0),
        "fetched_at": now_utc,
        "depth": int(getattr(comment, "depth", 0) or 0),
        "is_deleted": int(is_deleted),
        "raw_json": json_dump(
            {"id": comment.id, "parent_id": comment.parent_id}
            if not RAW_DUMP
            else {
                "id": comment.id,
                "parent_id": comment.parent_id,
                "body": body,
                "score": comment.score,
                "created_utc": comment.created_utc,
            }
        ),
    }


# --- Per-listing collection --------------------------------------------------


_LISTING_FETCHERS = {
    "hot":       lambda sub, limit: sub.hot(limit=limit),
    "new":       lambda sub, limit: sub.new(limit=limit),
    "rising":    lambda sub, limit: sub.rising(limit=limit),
    "top_week":  lambda sub, limit: sub.top(time_filter="week", limit=limit),
    "top_month": lambda sub, limit: sub.top(time_filter="month", limit=limit),
    "top_year":  lambda sub, limit: sub.top(time_filter="year", limit=limit),
}


def _iter_listing(subreddit: praw.models.Subreddit, listing: str, limit: int) -> Iterable[praw.models.Submission]:
    return _with_retry(_LISTING_FETCHERS[listing], subreddit, limit)


def _collect_comments(submission: praw.models.Submission, conn, now_utc: int) -> int:
    submission.comment_sort = "top"
    try:
        _with_retry(submission.comments.replace_more, limit=MORE_COMMENTS_EXPANSION)
    except Exception as exc:  # noqa: BLE001
        logger.warning("replace_more failed for %s: %s", submission.id, exc)
    kept = 0
    flat = submission.comments.list()[:MAX_COMMENTS_PER_POST]
    for comment in flat:
        if not isinstance(comment, praw.models.Comment):
            continue
        author = str(comment.author) if comment.author else "[deleted]"
        if author in IGNORE_AUTHORS:
            continue
        try:
            upsert_comment(conn, _comment_row(comment, submission.fullname, now_utc))
            kept += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("comment upsert failed (%s): %s", getattr(comment, "id", "?"), exc)
    return kept


def _dump_raw_listing(subreddit_name: str, listing: str, payload: list[dict[str, Any]]) -> None:
    if not RAW_DUMP:
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{subreddit_name}_{listing}_{int(time.time())}.json"
    path.write_text(json_dump(payload), encoding="utf-8")
    logger.debug("Dumped raw listing to %s", path)


def collect_subreddit(reddit: praw.Reddit, name: str, listings: Iterable[str] | None = None) -> dict[str, int]:
    """Collect a single subreddit across configured listings.

    Returns aggregate counters: {posts_seen, posts_kept, comments_kept, listings}.
    """
    listings = list(listings) if listings else list(LISTING_LIMITS.keys())
    totals = {"posts_seen": 0, "posts_kept": 0, "comments_kept": 0, "listings": 0}
    subreddit = _with_retry(reddit.subreddit, name)
    # Force-fetch metadata
    try:
        _with_retry(getattr, subreddit, "id")  # touches API
    except Exception as exc:  # noqa: BLE001
        logger.error("Cannot access r/%s: %s", name, exc)
        return totals

    now_utc = int(time.time())
    with get_conn() as conn:
        upsert_subreddit(
            conn,
            {
                "id": subreddit.fullname,
                "name": str(subreddit).lower(),
                "subscribers": int(getattr(subreddit, "subscribers", 0) or 0),
                "description": getattr(subreddit, "public_description", "") or "",
                "fetched_at": now_utc,
            },
        )

    for listing in listings:
        limit = LISTING_LIMITS.get(listing)
        if not limit:
            continue
        totals["listings"] += 1
        seen = kept_p = kept_c = 0
        with get_conn() as conn:
            log_id = log_collection_start(conn, name.lower(), listing, now_utc)
        error: str | None = None
        try:
            raw_dump_buf: list[dict[str, Any]] = []
            stream = _iter_listing(subreddit, listing, limit)
            pbar = tqdm(stream, total=limit, desc=f"r/{name}/{listing}", unit="post", leave=False)
            with get_conn() as conn:
                for i, submission in enumerate(pbar, start=1):
                    seen += 1
                    if not _post_passes_filters(submission):
                        continue
                    row = _post_row(submission, int(time.time()))
                    if RAW_DUMP:
                        raw_dump_buf.append(row)
                    try:
                        upsert_post(conn, row)
                        kept_p += 1
                        kept_c += _collect_comments(submission, conn, int(time.time()))
                        _harvest_mentions(conn, row["title"], row["selftext"], int(time.time()))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("post %s failed: %s", submission.id, exc)
                    if i % CHECKPOINT_EVERY == 0:
                        conn.commit()
                        logger.info(
                            "checkpoint r/%s/%s @ %d (kept %d posts / %d comments)",
                            name, listing, i, kept_p, kept_c,
                        )
            _dump_raw_listing(name.lower(), listing, raw_dump_buf)
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            logger.exception("Listing r/%s/%s failed: %s", name, listing, exc)
        finally:
            with get_conn() as conn:
                log_collection_finish(
                    conn,
                    log_id,
                    posts_seen=seen,
                    posts_kept=kept_p,
                    comments_kept=kept_c,
                    finished_at=int(time.time()),
                    error=error,
                )
        totals["posts_seen"]    += seen
        totals["posts_kept"]    += kept_p
        totals["comments_kept"] += kept_c

    logger.info(
        "r/%s done — seen=%d, posts kept=%d, comments kept=%d",
        name, totals["posts_seen"], totals["posts_kept"], totals["comments_kept"],
    )
    return totals


def _harvest_mentions(conn, title: str | None, body: str | None, now_utc: int) -> None:
    """Pluck `r/SubName` references from text and bump discovered_subreddits."""
    from config import SUBREDDIT_MENTION_RE  # local import to avoid cycle on init

    seen: set[str] = set()
    for src in (title, body):
        if not src:
            continue
        for m in SUBREDDIT_MENTION_RE.finditer(src):
            seen.add(m.group(1).lower())
    for sub in seen:
        bump_discovered(conn, sub, now_utc)


def collect_many(reddit: praw.Reddit, names: Iterable[str]) -> dict[str, dict[str, int]]:
    """Collect a list of subreddits sequentially. Returns per-sub counters."""
    results: dict[str, dict[str, int]] = {}
    for name in names:
        try:
            results[name] = collect_subreddit(reddit, name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Subreddit r/%s failed entirely: %s", name, exc)
            results[name] = {"posts_seen": 0, "posts_kept": 0, "comments_kept": 0, "listings": 0}
    return results
