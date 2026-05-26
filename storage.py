"""SQLite schema and CRUD helpers.

Designed so that re-running `collector.py` is idempotent (UPSERT by id) and
classifier/embedder results are preserved across re-collection.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS subreddits (
    id              TEXT PRIMARY KEY,        -- Reddit fullname (e.g. t5_xxx) or lowercased name
    name            TEXT NOT NULL UNIQUE,
    subscribers     INTEGER,
    description     TEXT,
    fetched_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id                  TEXT PRIMARY KEY,    -- t3_xxx
    subreddit_name      TEXT NOT NULL,
    title               TEXT,
    selftext            TEXT,
    url                 TEXT,
    author              TEXT,
    upvotes             INTEGER,
    downvote_ratio      REAL,
    num_comments        INTEGER,
    awards              INTEGER,
    created_utc         INTEGER NOT NULL,
    fetched_at          INTEGER NOT NULL,
    flair               TEXT,
    is_sticky           INTEGER,
    is_locked           INTEGER,
    is_deleted          INTEGER DEFAULT 0,
    hotness_score       REAL,
    engagement_ratio    REAL,
    pain_category       TEXT,
    goal_category       TEXT,
    knowledge_gap       TEXT,
    emotion             TEXT,
    life_stage          TEXT,
    cluster_id          INTEGER,
    classified_at       INTEGER,
    embedded_at         INTEGER,
    raw_json            TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_subreddit ON posts(subreddit_name);
CREATE INDEX IF NOT EXISTS idx_posts_created   ON posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_posts_pain      ON posts(pain_category);
CREATE INDEX IF NOT EXISTS idx_posts_goal      ON posts(goal_category);
CREATE INDEX IF NOT EXISTS idx_posts_cluster   ON posts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_posts_unclassified ON posts(classified_at) WHERE classified_at IS NULL;

CREATE TABLE IF NOT EXISTS comments (
    id                  TEXT PRIMARY KEY,    -- t1_xxx
    post_id             TEXT NOT NULL,
    parent_id           TEXT,
    author              TEXT,
    body                TEXT,
    upvotes             INTEGER,
    created_utc         INTEGER NOT NULL,
    fetched_at          INTEGER NOT NULL,
    depth               INTEGER,
    is_deleted          INTEGER DEFAULT 0,
    pain_category       TEXT,
    goal_category       TEXT,
    knowledge_gap       TEXT,
    emotion             TEXT,
    life_stage          TEXT,
    cluster_id          INTEGER,
    classified_at       INTEGER,
    embedded_at         INTEGER,
    raw_json            TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_utc);
CREATE INDEX IF NOT EXISTS idx_comments_unclassified ON comments(classified_at) WHERE classified_at IS NULL;

CREATE TABLE IF NOT EXISTS demographics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,           -- 'post' | 'comment'
    source_id       TEXT NOT NULL,
    age             INTEGER,
    gender          TEXT,
    income          TEXT,
    country         TEXT,
    life_stage      TEXT,
    extracted_at    INTEGER NOT NULL,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS discovered_subreddits (
    name            TEXT PRIMARY KEY,
    mention_count   INTEGER DEFAULT 0,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    relevance_score REAL,
    status          TEXT DEFAULT 'pending'   -- 'pending' | 'approved' | 'rejected'
);

CREATE TABLE IF NOT EXISTS collection_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subreddit_name  TEXT NOT NULL,
    listing         TEXT NOT NULL,           -- hot|top_week|new|rising|...
    posts_seen      INTEGER,
    posts_kept      INTEGER,
    comments_kept   INTEGER,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    error           TEXT
);
"""


def init_db(path: Path = DB_PATH) -> None:
    """Create schema if missing. Safe to call repeatedly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    logger.info("DB initialised at %s", path)


@contextmanager
def get_conn(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Yield a connection with WAL + row_factory configured. Commits on exit."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- UPSERTs -----------------------------------------------------------------


def upsert_subreddit(conn: sqlite3.Connection, sub: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO subreddits (id, name, subscribers, description, fetched_at)
        VALUES (:id, :name, :subscribers, :description, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            subscribers = excluded.subscribers,
            description = excluded.description,
            fetched_at  = excluded.fetched_at
        """,
        sub,
    )


def upsert_post(conn: sqlite3.Connection, post: dict[str, Any]) -> None:
    """UPSERT a post. Preserves classifier/embedder columns on conflict."""
    conn.execute(
        """
        INSERT INTO posts (
            id, subreddit_name, title, selftext, url, author,
            upvotes, downvote_ratio, num_comments, awards,
            created_utc, fetched_at, flair, is_sticky, is_locked, is_deleted,
            hotness_score, engagement_ratio, raw_json
        ) VALUES (
            :id, :subreddit_name, :title, :selftext, :url, :author,
            :upvotes, :downvote_ratio, :num_comments, :awards,
            :created_utc, :fetched_at, :flair, :is_sticky, :is_locked, :is_deleted,
            :hotness_score, :engagement_ratio, :raw_json
        )
        ON CONFLICT(id) DO UPDATE SET
            title            = excluded.title,
            selftext         = excluded.selftext,
            upvotes          = excluded.upvotes,
            downvote_ratio   = excluded.downvote_ratio,
            num_comments     = excluded.num_comments,
            awards           = excluded.awards,
            fetched_at       = excluded.fetched_at,
            flair            = excluded.flair,
            is_sticky        = excluded.is_sticky,
            is_locked        = excluded.is_locked,
            is_deleted       = excluded.is_deleted,
            hotness_score    = excluded.hotness_score,
            engagement_ratio = excluded.engagement_ratio,
            raw_json         = excluded.raw_json
        """,
        post,
    )


def upsert_comment(conn: sqlite3.Connection, comment: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO comments (
            id, post_id, parent_id, author, body, upvotes,
            created_utc, fetched_at, depth, is_deleted, raw_json
        ) VALUES (
            :id, :post_id, :parent_id, :author, :body, :upvotes,
            :created_utc, :fetched_at, :depth, :is_deleted, :raw_json
        )
        ON CONFLICT(id) DO UPDATE SET
            body         = excluded.body,
            upvotes      = excluded.upvotes,
            fetched_at   = excluded.fetched_at,
            is_deleted   = excluded.is_deleted,
            raw_json     = excluded.raw_json
        """,
        comment,
    )


def upsert_demographic(conn: sqlite3.Connection, demo: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO demographics (source_type, source_id, age, gender, income, country, life_stage, extracted_at)
        VALUES (:source_type, :source_id, :age, :gender, :income, :country, :life_stage, :extracted_at)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            age = excluded.age,
            gender = excluded.gender,
            income = excluded.income,
            country = excluded.country,
            life_stage = excluded.life_stage,
            extracted_at = excluded.extracted_at
        """,
        demo,
    )


def bump_discovered(conn: sqlite3.Connection, name: str, now_utc: int) -> None:
    conn.execute(
        """
        INSERT INTO discovered_subreddits (name, mention_count, first_seen, last_seen)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            mention_count = mention_count + 1,
            last_seen     = excluded.last_seen
        """,
        (name.lower(), now_utc, now_utc),
    )


def log_collection_start(
    conn: sqlite3.Connection, subreddit_name: str, listing: str, started_at: int
) -> int:
    cur = conn.execute(
        "INSERT INTO collection_log (subreddit_name, listing, started_at) VALUES (?, ?, ?)",
        (subreddit_name, listing, started_at),
    )
    return int(cur.lastrowid)


def log_collection_finish(
    conn: sqlite3.Connection,
    log_id: int,
    posts_seen: int,
    posts_kept: int,
    comments_kept: int,
    finished_at: int,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE collection_log
        SET posts_seen = ?, posts_kept = ?, comments_kept = ?, finished_at = ?, error = ?
        WHERE id = ?
        """,
        (posts_seen, posts_kept, comments_kept, finished_at, error, log_id),
    )


# --- Queries -----------------------------------------------------------------


def count_posts(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


def count_comments(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0])


def get_unclassified_posts(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, selftext FROM posts WHERE classified_at IS NULL LIMIT ?",
        (limit,),
    ).fetchall()


def json_dump(obj: Any) -> str:
    """Compact JSON for raw_json columns. Handles non-serialisable via str()."""
    return json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":"))
