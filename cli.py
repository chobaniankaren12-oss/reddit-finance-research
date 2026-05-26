"""CLI entry point.

Subcommands implemented now (Stage 1):
    setup    — create DB, check .env
    collect  — collect seeds or a single subreddit

Stubs (later stages):
    classify | embed | analyze | discover | dashboard
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import (
    ANTHROPIC_API_KEY,
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


def cmd_not_implemented(ns: argparse.Namespace) -> int:
    print(f"'{ns.command}' is not implemented yet (Stage 1 ships setup/collect only).")
    return 1


# --- Parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reddit_parser", description="Reddit research tool for EdTech finance product.")
    sub = p.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Initialise DB and check .env")
    p_setup.set_defaults(func=cmd_setup)

    p_collect = sub.add_parser("collect", help="Collect posts and comments")
    grp = p_collect.add_mutually_exclusive_group(required=True)
    grp.add_argument("--seeds", action="store_true", help="Use all seed subreddits from config.SEEDS")
    grp.add_argument("--sub", type=str, help="Single subreddit name (without r/ prefix)")
    p_collect.set_defaults(func=cmd_collect)

    for name in ("classify", "embed", "analyze", "discover", "dashboard"):
        sp = sub.add_parser(name, help=f"[stage 2+] {name}")
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
