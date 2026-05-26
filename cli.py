"""CLI entry point.

Subcommands implemented:
    setup        — create DB, check .env                          (Stage 1)
    collect      — PRAW-based collection (needs Reddit creds)     (Stage 1)
    collect-json — public JSON-endpoint collection, no auth       (Stage 1.5)
    classify     — regex demographics + Claude pain/goal labels   (Stage 2)

Stubs (later stages):
    embed | analyze | discover | dashboard
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import (
    ANTHROPIC_API_KEY,
    CLASSIFY_BATCH_SIZE,
    CLAUDE_MODEL,
    LOG_FILE,
    LOG_LEVEL,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    all_seeds,
)
from storage import count_comments, count_posts, get_conn, init_db


def _setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    # silence praw's chatty logger unless DEBUG
    if level > logging.DEBUG:
        logging.getLogger("prawcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


# --- Subcommand handlers -----------------------------------------------------


def cmd_setup(_: argparse.Namespace) -> int:
    log = logging.getLogger("setup")
    init_db()
    missing: list[str] = []
    if not REDDIT_CLIENT_ID:
        missing.append("REDDIT_CLIENT_ID")
    if not REDDIT_CLIENT_SECRET:
        missing.append("REDDIT_CLIENT_SECRET")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        log.warning("Missing env vars: %s — see .env.example", ", ".join(missing))
    else:
        log.info("All required env vars present")
    with get_conn() as conn:
        log.info("DB ready. posts=%d, comments=%d", count_posts(conn), count_comments(conn))
    return 0


def cmd_collect(ns: argparse.Namespace) -> int:
    log = logging.getLogger("collect")
    from collector import collect_many, make_reddit  # lazy import (heavy deps)

    reddit = make_reddit()
    if ns.sub:
        targets = [ns.sub]
    elif ns.seeds:
        targets = all_seeds()
    else:
        log.error("Provide either --seeds or --sub <name>")
        return 2

    log.info("Collecting %d subreddit(s): %s", len(targets), ", ".join(targets))
    results = collect_many(reddit, targets)

    total_p = sum(r["posts_kept"] for r in results.values())
    total_c = sum(r["comments_kept"] for r in results.values())
    log.info("=== TOTAL kept: %d posts, %d comments ===", total_p, total_c)
    for name, r in results.items():
        log.info(
            "  r/%-25s  seen=%-5d  posts=%-4d  comments=%-5d  listings=%d",
            name, r["posts_seen"], r["posts_kept"], r["comments_kept"], r["listings"],
        )
    return 0


def cmd_collect_json(ns: argparse.Namespace) -> int:
    """No-auth Reddit JSON collector. Same DB tables as PRAW collector."""
    log = logging.getLogger("collect-json")
    from collector_json import collect_many  # lazy import (requests + heavy)

    if ns.sub:
        targets = [ns.sub]
    elif ns.seeds:
        targets = all_seeds()
    else:
        log.error("Provide either --seeds or --sub <name>")
        return 2

    # Build (listing, limit, time_filter) tuples
    t_filter = ns.time if ns.listing == "top" else None
    listings = [(ns.listing, ns.limit, t_filter)]

    log.info(
        "Collecting %d subreddit(s) via JSON: %s | listing=%s limit=%d%s comments=%d",
        len(targets), ", ".join(targets), ns.listing, ns.limit,
        f" time={t_filter}" if t_filter else "", ns.comments_per_post,
    )
    results = collect_many(
        targets, listings,
        comments_per_post=ns.comments_per_post,
        min_comment_score=ns.min_comment_score,
        min_comment_length=ns.min_comment_length,
    )
    total_seen = sum(r["seen"] for r in results.values())
    total_kept = sum(r["kept"] for r in results.values())
    total_c = sum(r.get("comments", 0) for r in results.values())
    log.info(
        "=== TOTAL kept: %d posts / %d seen | %d comments ===",
        total_kept, total_seen, total_c,
    )
    for name, r in results.items():
        log.info(
            "  r/%-25s  seen=%-5d  kept=%-4d  comments=%-5d  listings=%d",
            name, r["seen"], r["kept"], r.get("comments", 0), r["listings"],
        )
    return 0


def cmd_classify(ns: argparse.Namespace) -> int:
    log = logging.getLogger("classify")
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY missing — fill .env first.")
        return 2
    from classifier import run_classifier, run_comments_classifier

    total_errors = 0

    if ns.target in ("posts", "both"):
        log.info("--- Classifying POSTS ---")
        cp = run_classifier(
            batch_size=ns.batch_size, limit=ns.limit, model=ns.model,
            skip_keyword_filter=ns.no_keyword_filter,
        )
        log.info(
            "Posts: signal=%d  no_signal=%d  demographics=%d  errors=%d",
            cp["signal"], cp["no_signal"], cp["demographics"], cp["errors"],
        )
        total_errors += cp["errors"]

    if ns.target in ("comments", "both"):
        log.info("--- Classifying COMMENTS ---")
        cc = run_comments_classifier(
            batch_size=ns.comments_batch_size, limit=ns.limit, model=ns.model,
            skip_keyword_filter=ns.no_keyword_filter,
        )
        log.info(
            "Comments: signal=%d  no_signal=%d  demographics=%d  errors=%d",
            cc["signal"], cc["no_signal"], cc["demographics"], cc["errors"],
        )
        total_errors += cc["errors"]

    return 0 if total_errors == 0 else 1


def cmd_not_implemented(ns: argparse.Namespace) -> int:
    print(f"'{ns.command}' is not implemented yet.")
    return 1


# --- Parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reddit_parser", description="Reddit research tool for EdTech finance product.")
    sub = p.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Initialise DB and check .env")
    p_setup.set_defaults(func=cmd_setup)

    p_collect = sub.add_parser("collect", help="Collect posts and comments (PRAW)")
    grp = p_collect.add_mutually_exclusive_group(required=True)
    grp.add_argument("--seeds", action="store_true", help="Use all seed subreddits from config.SEEDS")
    grp.add_argument("--sub", type=str, help="Single subreddit name (without r/ prefix)")
    p_collect.set_defaults(func=cmd_collect)

    p_json = sub.add_parser(
        "collect-json",
        help="Collect via public JSON endpoints (no Reddit auth, no comments)",
    )
    jgrp = p_json.add_mutually_exclusive_group(required=True)
    jgrp.add_argument("--seeds", action="store_true", help="All seed subreddits")
    jgrp.add_argument("--sub", type=str, help="Single subreddit name")
    p_json.add_argument(
        "--listing", type=str, default="hot",
        choices=["hot", "new", "top", "rising"],
        help="Which listing to fetch (default: hot)",
    )
    p_json.add_argument(
        "--limit", type=int, default=100,
        help="Posts per listing, max ~1000 (default: 100)",
    )
    p_json.add_argument(
        "--time", type=str, default="week",
        choices=["hour", "day", "week", "month", "year", "all"],
        help="Time filter — only applies to --listing top (default: week)",
    )
    p_json.add_argument(
        "--comments-per-post", type=int, default=0,
        help="Top-N comments to fetch per post (0 = skip, default). Adds 1 request per post.",
    )
    p_json.add_argument(
        "--min-comment-score", type=int, default=1,
        help="Drop comments with score below this (default: 1, keeps everything ≥1)",
    )
    p_json.add_argument(
        "--min-comment-length", type=int, default=50,
        help="Drop comments shorter than this (chars). Default 50 — filters out 'yeah', '+1' etc.",
    )
    p_json.set_defaults(func=cmd_collect_json)

    p_classify = sub.add_parser(
        "classify",
        help="Classify unclassified posts (regex demographics + Claude labels)",
    )
    p_classify.add_argument(
        "--batch-size", type=int, default=CLASSIFY_BATCH_SIZE,
        help=f"Posts per Claude call (default: {CLASSIFY_BATCH_SIZE})",
    )
    p_classify.add_argument(
        "--limit", type=int, default=None,
        help="Cap total posts processed in this run (default: all unclassified)",
    )
    p_classify.add_argument(
        "--model", type=str, default=CLAUDE_MODEL,
        help=f"Anthropic model id (default: {CLAUDE_MODEL})",
    )
    p_classify.add_argument(
        "--target", choices=["posts", "comments", "both"], default="both",
        help="Which rows to classify (default: both)",
    )
    p_classify.add_argument(
        "--comments-batch-size", type=int, default=20,
        help="Batch size for comments (default 20; expanded schema → ~250 output tokens/item)",
    )
    p_classify.add_argument(
        "--no-keyword-filter", action="store_true",
        help="Bypass the keyword pre-filter — send every row to Claude (more expensive but catches implicit signals).",
    )
    p_classify.set_defaults(func=cmd_classify)

    for name in ("embed", "analyze", "discover", "dashboard"):
        sp = sub.add_parser(name, help=f"[stage 3+] {name}")
        sp.set_defaults(func=cmd_not_implemented)
        # accept arbitrary args so stubs don't choke
        sp.add_argument("rest", nargs=argparse.REMAINDER)

    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = build_parser()
    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
