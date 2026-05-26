"""Insert hand-crafted synthetic posts into the local DB for testing.

Why this exists:
    Lets you smoke-test `classifier.py` end-to-end without needing Reddit API
    credentials. Run AFTER `python cli.py setup` (which creates the schema).
    Inserts ~10 posts covering: clear pain signals, clear goals, knowledge
    gaps, demographic mentions, and a couple of "no signal" controls.

Usage:
    python cli.py setup           # create DB if missing
    python seed_synthetic.py      # insert 10 synthetic posts
    python cli.py classify        # run classifier on them
"""
from __future__ import annotations

import logging
import time

from storage import get_conn, upsert_post, upsert_subreddit

logger = logging.getLogger(__name__)


SYNTHETIC_SUBREDDIT = {
    "id": "t5_synthetic",
    "name": "synthetic_test",
    "subscribers": 0,
    "description": "Synthetic posts for classifier smoke testing.",
}


# 10 posts covering the full label space + edge cases.
SYNTHETIC_POSTS: list[dict[str, str]] = [
    {
        "id": "t3_syn_001",
        "title": "I'm 27M making $80k and living paycheck to paycheck — what am I doing wrong?",
        "selftext": (
            "Been working full-time for 3 years, salary is decent but I have nothing "
            "saved. Credit card balance is $12k and growing. Feel completely overwhelmed. "
            "How do I even start digging out of this hole?"
        ),
    },
    {
        "id": "t3_syn_002",
        "title": "ELI5: what is the difference between an ETF and a mutual fund?",
        "selftext": (
            "I'm 22F in Germany and totally new to investing. Want to learn the basics "
            "before I put any money in. Noob question I know but please be gentle."
        ),
    },
    {
        "id": "t3_syn_003",
        "title": "35 years old, salary of $120,000, no retirement savings",
        "selftext": (
            "Wish I knew this stuff 10 years ago. Now I'm scared I'm too late to retire "
            "comfortably. UK-based. Should I max out a SIPP first or pay down the mortgage?"
        ),
    },
    {
        "id": "t3_syn_004",
        "title": "Just hit Coast FIRE at 41! Sharing my numbers",
        "selftext": (
            "After 15 years of grinding I can finally relax. Goal was financial freedom "
            "by 45 — beat it by 4 years. Net worth $750k, mostly in VTSAX. Feels surreal."
        ),
    },
    {
        "id": "t3_syn_005",
        "title": "Tuesday daily discussion thread",
        "selftext": (
            "What's everyone up to today? Market is flat. I bought some VOO this morning."
        ),
    },
    {
        "id": "t3_syn_006",
        "title": "Drowning in student loans, behind on rent — 23F in Canada",
        "selftext": (
            "Income of $45k, loan balance $80k. Can't figure out how to make a budget "
            "that actually works. Feel stupid for taking on this much debt."
        ),
    },
    {
        "id": "t3_syn_007",
        "title": "Recommend a book for someone wanting to learn the basics of investing",
        "selftext": (
            "I'm a beginner — first time looking at any of this. Heard about Bogleheads "
            "and Boglehead's Guide. Is it worth reading or outdated?"
        ),
    },
    {
        "id": "t3_syn_008",
        "title": "Weekly check-in: what did you save this week?",
        "selftext": (
            "Post your saves below. I put $200 into my emergency fund and $500 into "
            "VTSAX. Nothing crazy this week."
        ),
    },
    {
        "id": "t3_syn_009",
        "title": "Should I take a HELOC to pay off my credit card debt?",
        "selftext": (
            "37F earning $200,000 a year, $25k in credit card debt at 22% APR. "
            "Dumb question but I really need help — would converting to HELOC at 8% "
            "actually save me money long-term or am I trading one trap for another?"
        ),
    },
    {
        "id": "t3_syn_010",
        "title": "Lifestyle creep is destroying my savings rate",
        "selftext": (
            "Got a $30k raise last year. Two years ago I was saving 25% of income. "
            "Now I'm saving 8% and have no idea where the money is going. Embarrassed "
            "that I let this happen."
        ),
    },
]


def seed() -> int:
    now = int(time.time())
    with get_conn() as conn:
        # Subreddit row first (FK target).
        upsert_subreddit(conn, {**SYNTHETIC_SUBREDDIT, "fetched_at": now})
        n = 0
        for post in SYNTHETIC_POSTS:
            text = f"{post['title']} {post['selftext']}"
            row = {
                "id": post["id"],
                "subreddit_name": SYNTHETIC_SUBREDDIT["name"],
                "title": post["title"],
                "selftext": post["selftext"],
                "url": f"https://example.invalid/{post['id']}",
                "author": "synthetic_user",
                "upvotes": 42,
                "downvote_ratio": 0.95,
                "num_comments": 7,
                "awards": 0,
                "created_utc": now - (3600 * (n + 1)),
                "fetched_at": now,
                "flair": None,
                "is_sticky": 0,
                "is_locked": 0,
                "is_deleted": 0,
                "hotness_score": 42 * 0.3 + 7 * 0.5,
                "engagement_ratio": 7 / 42,
                "raw_json": "{}",
            }
            upsert_post(conn, row)
            n += 1
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    inserted = seed()
    print(f"Inserted/upserted {inserted} synthetic posts into r/synthetic_test")
    print("Next: python cli.py classify --limit 20")
