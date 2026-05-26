"""Project configuration: seed subreddits, keyword clusters, collection limits."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT: Final[Path] = Path(__file__).parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
RAW_DIR: Final[Path] = DATA_DIR / "raw"
DB_PATH: Final[Path] = PROJECT_ROOT / os.getenv("DB_PATH", "data/reddit.db")

LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: Final[Path] = PROJECT_ROOT / "logs" / "reddit_parser.log"
RAW_DUMP: Final[bool] = os.getenv("RAW_DUMP", "0") == "1"

REDDIT_CLIENT_ID: Final[str | None] = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET: Final[str | None] = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT: Final[str] = os.getenv(
    "REDDIT_USER_AGENT", "reddit_parser/0.1 by u/unknown"
)

ANTHROPIC_API_KEY: Final[str | None] = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL: Final[str] = "claude-sonnet-4-6"
CLASSIFY_BATCH_SIZE: Final[int] = 20

# --- Seed subreddits (layered) -----------------------------------------------
SEEDS: Final[dict[str, list[str]]] = {
    "A_newbies_money_fear": [
        "personalfinance",
        "povertyfinance",
        "MiddleClassFinance",
        "StudentLoans",
        "Frugal",
        "FinancialPlanning",
        "Money",
    ],
    "B_learning_to_invest": [
        "Bogleheads",
        "investing",
        "dividends",
        "ETFs",
        "Fire",
        "leanfire",
        "coastFIRE",
        "eupersonalfinance",
    ],
    "C_psychology_habits": [
        "DebtFree",
        "ynab",
        "financialindependence",
        "FinancialIndependence",
    ],
}


def all_seeds() -> list[str]:
    """Flat de-duplicated list of seed subreddit names (case-insensitive)."""
    seen: dict[str, str] = {}
    for group in SEEDS.values():
        for name in group:
            seen.setdefault(name.lower(), name)
    return list(seen.values())


# --- Collection limits -------------------------------------------------------
LISTING_LIMITS: Final[dict[str, int]] = {
    "hot": 100,
    "top_week": 100,
    "top_month": 100,
    "top_year": 100,
    "new": 100,
    "rising": 50,
}

MAX_COMMENTS_PER_POST: Final[int] = 500
# replace_more(limit=N): how many MoreComments nodes to expand. Each = 1 API call.
MORE_COMMENTS_EXPANSION: Final[int] = 16

# Noise filters
MIN_UPVOTES: Final[int] = 10
MIN_COMMENTS: Final[int] = 5
# Posts must pass at least one threshold (NOT both — generous on either signal).
IGNORE_AUTHORS: Final[frozenset[str]] = frozenset({"AutoModerator", "[deleted]"})

# Checkpoint cadence
CHECKPOINT_EVERY: Final[int] = 50

# Rate limiting / retry
RETRY_MAX_ATTEMPTS: Final[int] = 5
RETRY_BASE_DELAY: Final[float] = 2.0  # seconds, exponential

# --- Keyword clusters --------------------------------------------------------
# Substrings are matched case-insensitively against title+selftext (posts)
# or body (comments). A match flags the item as "interesting" for the LLM stage.

PAIN_KEYWORDS: Final[list[str]] = [
    "i don't understand",
    "i dont understand",
    "confused about",
    "scared of",
    "too late to",
    "wish i knew",
    "struggling with",
    "can't figure out",
    "cant figure out",
    "overwhelmed",
    "feel stupid",
    "embarrassed",
    "living paycheck",
    "paycheck to paycheck",
    "in debt",
    "behind on",
    "drowning in",
]

GOAL_KEYWORDS: Final[list[str]] = [
    "want to learn",
    "how do i start",
    "first time",
    "beginner",
    "basics of",
    "explain like",
    "recommend a book",
    "trying to",
    "goal is",
    "by age",
    "financial freedom",
    "retire by",
    "fire by",
]

GAP_KEYWORDS: Final[list[str]] = [
    "what is the difference between",
    "is it worth it",
    "is x worth it",
    "should i",
    "eli5",
    "dumb question",
    "noob question",
    "stupid question",
    "newbie question",
]

_ALL_KEYWORDS = PAIN_KEYWORDS + GOAL_KEYWORDS + GAP_KEYWORDS


def has_signal(text: str | None) -> bool:
    """Return True if any keyword cluster fires on the text."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _ALL_KEYWORDS)


# --- Demography regexes ------------------------------------------------------
# Compiled once at import. Used by classifier.py later (Этап 2).
AGE_GENDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:i'?m\s+|i am\s+)?(\d{2})\s*([MFmf])\b"
)
AGE_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:i'?m|i am|im)\s+(\d{2})\b|\b(\d{2})\s+years?\s+old\b",
    re.IGNORECASE,
)
INCOME_RE: Final[re.Pattern[str]] = re.compile(
    r"\$\s?(\d{2,3})\s?[kK]\b|making\s+\$?(\d{2,6})|salary of\s+\$?(\d{2,6})"
)
COUNTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bin (?:the )?(US|USA|UK|EU|Germany|Canada|Australia|France|Netherlands|Spain|Italy|Poland|Ireland)\b"
)

# Cross-post mention regex: r/SubName or /r/SubName
SUBREDDIT_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s(])/?r/([A-Za-z0-9_]{2,21})\b")
