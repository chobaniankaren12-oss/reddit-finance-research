# reddit-finance-research

> Personal-use, read-only research tool for analysing public discussions in
> personal-finance subreddits. Built to help me better understand common
> questions and pain points as I'm learning about consumer finance and
> exploring ideas for a personal-finance education product.

## For Reddit API reviewers

**This is a personal-use script, not a service for other Redditors.**

| Aspect | Detail |
|---|---|
| Operator | Single individual, personal use |
| Reddit interaction | **Read-only** via official PRAW client. No posting, no voting, no commenting, no messaging |
| Data scope | Public posts and comments only, from subreddits listed below |
| Storage | Local SQLite file on my machine |
| Data sharing | **None.** Nothing is redistributed, resold, or shared externally |
| User profiles | Public usernames are stored only for attribution of the post/comment they wrote. No profile scraping beyond that |
| NSFW / private | Not collected. No DMs, no private subreddits |
| Volume | ~100–500 requests/day, run manually on demand. Well below the 100 req/min limit |
| Cadence | Manual runs only, not a continuous service or bot |

### What the tool does

For each configured subreddit, it pulls a snapshot of public posts (hot / top /
new / rising) and their comments using PRAW. Locally, it:

1. Stores the text in SQLite with metadata (score, comment count, timestamp).
2. Runs keyword + LLM classification (via Anthropic Claude API, off Reddit) to
   tag each post as a "pain", "goal", or "knowledge gap" — these are mental
   categories I use to organise themes for my own learning.
3. Generates embeddings and clusters them with HDBSCAN to surface recurring
   discussion topics.
4. Produces CLI reports and a local Streamlit dashboard for personal use.

### What the tool explicitly does NOT do

- Does not post, vote, comment, or send messages on Reddit.
- Does not scrape user profiles beyond the username attached to a public post.
- Does not collect, store, or process NSFW subreddits or private content.
- Does not redistribute, resell, share, or publish any Reddit data.
- Does not run continuously / as a daemon. Each run is manual.
- Does not use unofficial APIs, web scraping, or HTML parsing. PRAW only.

### Subreddits accessed

Public personal-finance communities only:

`r/personalfinance`, `r/povertyfinance`, `r/MiddleClassFinance`,
`r/StudentLoans`, `r/Frugal`, `r/FinancialPlanning`, `r/Money`,
`r/Bogleheads`, `r/investing`, `r/dividends`, `r/ETFs`, `r/Fire`,
`r/leanfire`, `r/coastFIRE`, `r/eupersonalfinance`, `r/DebtFree`,
`r/ynab`, `r/financialindependence`, `r/FinancialIndependence`.

### Why not Devvit?

- Off-Reddit data processing (local Python environment) is required for the
  NLP pipeline (sentence-transformers, HDBSCAN, Anthropic API).
- Local SQLite storage for offline analysis.
- No interaction with Redditors on the platform — Devvit is designed for
  in-platform apps, which is the opposite of this use case.

---

## Tech stack

- Python 3.11+
- PRAW (official Reddit API client, read-only)
- SQLite (local file, WAL mode)
- sentence-transformers (`all-MiniLM-L6-v2`)
- HDBSCAN
- Anthropic Claude API (off-Reddit classification)
- Streamlit (local dashboard)

## Project layout

```
reddit-finance-research/
├── config.py        # seed subreddits, keyword clusters, collection limits
├── storage.py       # SQLite schema + idempotent UPSERTs
├── collector.py     # PRAW collection with retry + checkpoints
├── classifier.py    # [stage 2] Claude-based Pain/Goal/Gap tagging
├── embedder.py      # [stage 3] embeddings + HDBSCAN clustering
├── analyzer.py      # [stage 4] CLI reports
├── discovery.py     # [stage 5] surface related subreddits
├── dashboard.py     # [stage 6] local Streamlit UI
├── cli.py           # entry point
└── data/
    └── reddit.db    # local SQLite (gitignored)
```

## Stage status

- [x] **Stage 1** — config, storage, collector, CLI (`setup`, `collect`)
- [x] **Stage 2** — Claude classifier + demography regex (`classify`)
- [ ] Stage 3 — embeddings + HDBSCAN clustering
- [ ] Stage 4 — analyzer (CLI reports)
- [ ] Stage 5 — related-subreddit discovery
- [ ] Stage 6 — Streamlit dashboard

## Setup (for the operator)

```bash
git clone https://github.com/<user>/reddit-finance-research
cd reddit-finance-research
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in Reddit and Anthropic credentials
python cli.py setup
```

### Reddit credentials

1. Open https://www.reddit.com/prefs/apps
2. Create app → type: **script**
3. Copy the client ID (under app name) and secret into `.env`
4. Set `REDDIT_USER_AGENT` to something descriptive — Reddit requires identification.

### First run

```bash
python cli.py collect --sub personalfinance     # one subreddit, sanity check
python cli.py collect --seeds                   # full sweep across all seeds
python cli.py classify --limit 50               # tag a small sample first
python cli.py classify                          # tag everything unclassified
```

### Classifier notes

- Posts that don't match any keyword in `PAIN_KEYWORDS`/`GOAL_KEYWORDS`/`GAP_KEYWORDS`
  (see `config.py`) are marked `pain=goal='none'` without a Claude call (cheap pass).
- Posts that match at least one keyword are sent to Claude in batches of `--batch-size`
  (default 20). System prompt is cache-flagged → subsequent batches are cheaper.
- Demographics (age/gender/income/country) are extracted via regex for every post and
  written to the `demographics` table; `life_stage` comes from the LLM.
- Re-running is idempotent: `posts.classified_at` gates which rows the next run touches.

## Database

SQLite at `data/reddit.db`. Schema lives in `storage.py`. Re-running `collect`
is idempotent — posts/comments UPSERT on Reddit `id` and never duplicate.
Classifier and clustering columns are preserved across re-fetches.

```bash
sqlite3 data/reddit.db "SELECT subreddit_name, COUNT(*) FROM posts GROUP BY 1;"
```

## Design notes

- **PRAW only.** No HTML scraping, no unofficial endpoints.
- **No PII** beyond public Reddit usernames already attached to public posts.
- **Idempotent.** Re-collection does not duplicate data and preserves
  classifier/cluster fields.
- **Cheap retries.** Exponential backoff on transient PRAW errors; 403/404
  abort fast so the tool respects access controls.
- **Noise filter.** Posts pass if they have either ≥10 upvotes OR ≥5 comments.
- **Checkpoints.** Connection commits every 50 posts.

## License

This repository contains personal research code. No data from Reddit is
included in the repo or distributed with it.
