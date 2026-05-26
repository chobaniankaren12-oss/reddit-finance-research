# reddit-finance-research

> Social-media research tool for US personal-finance subreddits.
> Collects Reddit posts + top comments, classifies them with Claude into a
> rich UA-research schema (pains, goals, demographics, intents, buying signals,
> brand mentions, trigger events…), and exports a multi-sheet Excel report
> ready for product/marketing analysis.
>
> Built for EdTech / fintech founders, marketers, and researchers doing
> user-acquisition discovery. Runs locally on your machine — your data and
> API costs stay yours.

---

## 🚀 Quick start with Claude Code

If you use [Claude Code](https://claude.com/claude-code), the fastest path is
to clone this repo, open it in Claude Code, and paste:

> Read README.md and walk me through the setup. Help me configure `.env`,
> install dependencies, and run a small end-to-end test on r/personalfinance.

Claude Code will handle the brew/venv/pip steps, prompt you for API keys, and
verify each command works. Skip to **Manual setup** below if you'd rather do
it yourself.

---

## 🛠 Manual setup (5 minutes)

```bash
# 1. Clone
git clone https://github.com/chobaniankaren12-oss/reddit-finance-research
cd reddit-finance-research

# 2. Python 3.11+ (3.9 is NOT enough — uses modern typing)
brew install python@3.11        # or pyenv / apt as appropriate
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install deps
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure secrets
cp .env.example .env
# Edit .env with a text editor:
#   ANTHROPIC_API_KEY=sk-ant-...   (required)
#   REDDIT_CLIENT_ID=...           (optional — for PRAW collector)
#   REDDIT_CLIENT_SECRET=...
#   REDDIT_USER_AGENT=...

# 5. Initialise the local SQLite DB
python cli.py setup
```

You also need **rclone** if you want to push the Excel report to Google Drive:

```bash
brew install rclone
rclone config       # set up a remote named 'gdrive' (type: drive)
```

---

## 🎬 30-minute end-to-end demo (no Reddit credentials needed)

```bash
# A. Collect ~100 hot posts + 30 top comments per post from 3 large subs
#    via Reddit's public JSON endpoints (no auth needed, ~12 min wall time)
for sub in personalfinance povertyfinance Bogleheads; do
  python cli.py collect-json --sub "$sub" --listing top --time week \
                              --limit 100 --comments-per-post 30
done

# B. Classify ~300 posts with the full schema (Claude sonnet, ~$1.50, ~10 min)
python cli.py classify --target posts --no-keyword-filter \
                       --model claude-sonnet-4-6

# C. Classify ~1500 top-upvoted comments (haiku, ~$1.50, ~20 min)
python cli.py classify --target comments --no-keyword-filter \
                       --limit 1500 \
                       --model claude-haiku-4-5-20251001

# D. Build the multi-sheet Excel report
python build_report.py                 # local file only
# or
python build_report.py --upload --replace   # also push to Drive
```

Expected cost: **~$3-5 in Claude API credits**. Reddit JSON access is free.

---

## 🎯 What you get

The exported `reports/reddit_research_<date>.xlsx` has **18 sheets**:

| Sheet | What it tells you |
|---|---|
| **Executive Summary** | KPIs (post count, classified, quotables) + top-5 pains/goals + donut chart |
| **Buying Signals** | UA lead segmentation: 🔥 hot (ready to pay) → 🔵 cold (just venting). Sample HOT posts |
| **Trigger Events** | Which life events motivate posts (job_change, medical_event, new_baby…) → ad timing |
| **UA Personas** | life_stage × financial_experience × employment_status segments with top pain/goal/trigger |
| **Brand Mentions** | Competitors, influencers, apps mentioned (Fidelity, Dave_Ramsey, YNAB, etc.) |
| **Interest Map** | 401k, ETFs, crypto, FIRE — which topics co-occur with which pain |
| **Intent Library** | Concrete "what they want to do next" statements grouped by goal |
| **Top Pains** | pain × subreddit cross-tab with color scale |
| **Top Goals** | goal × subreddit cross-tab |
| **Verbatims** | Quotable posts ready for marketing copy, sortable by signal/trigger/stage |
| **Knowledge Gaps** | Unique "what they don't understand" questions → content plan |
| **Demographics** | Age-bucket × pain, country × pain (regex-extracted age/gender/income) |
| **Subreddit Profiles** | Per-sub: top pain, top goal, % quotable, top emotion |
| **Top Comments** | High-upvote classified comments with their pain category |
| **Comments × Subreddit** | Comment-level pain cross-tab |
| **Top Conversations** | Post + its top-3 child comments together, for context |
| **Trends** | Post date distribution (becomes meaningful after multiple weekly runs) |
| **Raw Data** | Full export, 25 columns per post, with auto-filters |

---

## 🧠 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ 1. COLLECT  →  Reddit JSON public endpoints (no auth)             │
│               OR PRAW (needs Reddit script app)                   │
│               → posts + top-N comments into SQLite                │
├──────────────────────────────────────────────────────────────────┤
│ 2. CLASSIFY →  For each post/comment:                             │
│               • regex demographics (age/gender/income/country)    │
│               • keyword pre-filter (optional, --no-keyword-filter)│
│               • Claude batch call (sonnet for posts, haiku for    │
│                 comments) → 18-field structured output            │
├──────────────────────────────────────────────────────────────────┤
│ 3. REPORT   →  18-sheet Excel via openpyxl, optional Drive push   │
└──────────────────────────────────────────────────────────────────┘
```

**Why both PRAW and JSON collectors?**
PRAW needs an approved Reddit script app (approval takes weeks). JSON
endpoints work immediately but don't fetch comments via the same call,
have stricter rate limits, and cap at 1000 items per listing. Start with
JSON; switch to PRAW when approved — DB schema is identical.

---

## 📋 Command reference

```bash
python cli.py setup
  # Create / migrate the local SQLite schema. Idempotent.

python cli.py collect --sub <name>          # via PRAW (needs Reddit creds)
python cli.py collect --seeds
python cli.py collect-json --sub <name>     # via public JSON (no auth)
python cli.py collect-json --seeds          # all 18 seed subreddits
  Options:
    --listing {hot,new,top,rising}    Default: hot
    --limit N                          Posts per listing. Default 100, max ~1000.
    --time {hour,day,week,month,year,all}
                                       Only with --listing top. Default: week
    --comments-per-post N              0 (default) = skip; >0 = fetch top-N comments
    --min-comment-score N              Drop comments below this score. Default 1.
    --min-comment-length N             Drop comments shorter than N chars. Default 50.

python cli.py classify
  Options:
    --target {posts,comments,both}     Default: both
    --batch-size N                     Posts per Claude call. Default 15.
    --comments-batch-size N            Comments per Claude call. Default 20.
    --limit N                          Cap rows per target this run.
    --model <model-id>                 Default: claude-sonnet-4-6
                                       Use claude-haiku-4-5-20251001 for ~5× cheaper
    --no-keyword-filter                Bypass the cheap regex pre-filter; send
                                       every row to Claude. RECOMMENDED for
                                       research — keyword list misses implicit signal.

python build_report.py
  Options:
    --output <path>                    Default: reports/reddit_research_YYYY-MM-DD.xlsx
    --upload                           Push to Drive via rclone gdrive: remote
    --replace                          Overwrite existing file (preserves Drive URL/ID)

python seed_synthetic.py
  Insert 10 hand-crafted posts for smoke-testing without Reddit collection.
```

---

## 🏷 Classification schema (what gets captured per post/comment)

### Core labels
- **pain_category** (18 values): `debt`, `no_savings`, `fear_of_investing`, `lifestyle_inflation`, `retirement_anxiety`, `financial_illiteracy`, `family_pressure`, `income_low`, `housing_stress`, `healthcare_cost`, `job_insecurity`, `uncertain_future`, `regret_past_decisions`, `relationship_money_tension`, `tax_anxiety`, `credit_score_anxiety`, `other`, `none`
- **goal_category** (17 values): `start_investing`, `build_emergency_fund`, `pay_off_debt`, `retire_early`, `buy_house`, `increase_income`, `learn_basics`, `budgeting`, `save_for_kids`, `financial_independence`, `leave_inheritance`, `career_change`, `build_credit_score`, `tax_optimization`, `build_side_income`, `other`, `none`
- **emotion** (15 values): `anxious`, `frustrated`, `hopeful`, `ashamed`, `confused`, `motivated`, `determined`, `overwhelmed`, `regretful`, `guilty`, `envious`, `relieved`, `proud`, `fearful`, `neutral`
- **knowledge_gap**: free text, ≤12 words
- **quote_worthy**: boolean — quotable for marketing copy

### User profile
- **life_stage**: `student`, `early_career`, `mid_career`, `parent`, `near_retirement`, `retired`, `unknown`
- **financial_experience**: `beginner`, `intermediate`, `advanced`, `unknown`
- **employment_status**: `W2_employee`, `contractor_or_gig`, `self_employed`, `unemployed`, `retired`, `student`, `government_or_military`, `unknown`
- **family_status**: `single`, `partnered_unmarried`, `married_no_kids`, `married_with_kids`, `single_parent`, `divorced`, `caregiver_aging_parent`, `unknown`

### Intent & interests
- **intent**: free text, ≤8 words. The immediate next action they want to take.
- **interests**: multi-tag from `401k`, `roth_ira`, `traditional_ira`, `hsa`, `fsa`, `529_plan`, `stocks`, `bonds`, `etfs`, `mutual_funds`, `index_funds`, `dividends`, `real_estate`, `REITs`, `rental_property`, `crypto`, `FIRE_movement`, `side_hustle`, `gig_work`, `frugal_living`, `debt_payoff`, `budgeting_apps`, `credit_score`, `credit_cards`, `mortgage`, `refinancing`, `HELOC`, `student_loans`, `taxes`, `tax_optimization`, `social_security`, `medicare`, `pension`, `health_insurance`, `life_insurance`, `career_change`, `salary_negotiation`

### UA signals (the most actionable for marketing)
- **trigger_event**: `job_loss`, `job_change`, `got_raise_or_bonus`, `new_baby`, `marriage`, `divorce`, `death_in_family`, `inheritance`, `home_purchase`, `home_sale`, `relocation`, `kid_starting_college`, `approaching_retirement`, `just_retired`, `medical_event`, `debt_paid_off_milestone`, `investment_gain_or_loss`, `started_side_hustle`, `none`, `unknown`
- **buying_signal**: `paying_for_solution_now` (🔥), `actively_looking_for_resource` (🟠), `asking_for_advisor` (🟠), `research_phase` (🟡), `no_buying_signal` (🔵), `unknown`
- **urgency**: `immediate_crisis`, `short_term_planning`, `long_term_planning`, `just_exploring`, `unknown`
- **mentioned_brands**: free-text tags. Hint list: `Dave_Ramsey`, `Suze_Orman`, `Caleb_Hammer`, `Money_Guy`, `Mr_Money_Mustache`, `Ramit_Sethi`, `Vanguard`, `Fidelity`, `Schwab`, `Robinhood`, `YNAB`, `Mint`, `Rocket_Money`, `Personal_Capital`, `Empower`, `Betterment`, `Wealthfront`, `Coinbase`, `NerdWallet`, etc.

Plus regex-extracted demographics: `age`, `gender`, `income`, `country` (in a separate `demographics` table).

---

## 💰 Cost expectations (Anthropic API)

Rough numbers as of late 2025 pricing:

| Workload | Model | Cost |
|---|---|---|
| 500 posts, full UA schema | claude-sonnet-4-6 | ~$2.50 |
| 500 posts, full UA schema | claude-haiku-4-5 | ~$0.50 |
| 2000 comments, full UA schema | claude-sonnet-4-6 | ~$6 |
| 2000 comments, full UA schema | claude-haiku-4-5 | ~$1.50 |

**Recommended hybrid:** sonnet for posts (verbatims need high quality), haiku
for comments (volume matters more than per-item quality for aggregations).

Reddit JSON access is free. PRAW also free but rate-limited and needs an
approved script app.

---

## 🔍 Design notes / gotchas

This section captures real lessons from building this pipeline. They will save
you time.

**1. The keyword pre-filter is intentionally narrow.**
`config.PAIN_KEYWORDS`/`GOAL_KEYWORDS`/`GAP_KEYWORDS` catch ~33 obvious
patterns ("struggling with", "ELI5", "how do I start"). It exists to save
API cost on chitchat. But it misses ~70% of implicit pain expressions
("laid off", "behind on rent", "cooked on retirement"). **For real
research, always use `--no-keyword-filter`** — pay the ~5× extra and let
Claude judge every item.

**2. Comments have a much lower signal rate than posts.**
Posts are people asking for help (high signal). Comments are conversational
("yeah, same", "thanks for the advice", anecdotes). Expect ~10-20% of
comments to carry strong pain/goal signal even with `--no-keyword-filter`.
Filter by `min-comment-score` and `min-comment-length` to skip the noise.

**3. Demographics regex is naive — context matters.**
`config.INCOME_RE` matches `$80k` and `$120,000` correctly. But it falsely
captures `$750k net worth` and `$30k raise` as income because it doesn't
distinguish income from balance/raise/inheritance. Treat the
`demographics.income` field as approximate; cross-check with the post text.

**4. Output token limits matter with rich schemas.**
The current 18-field schema produces ~250 output tokens per item. With
`max_tokens=8192` and batch sizes of 15 (posts) / 20 (comments), batches
finish in one Claude call. If you expand the schema further, drop the batch
size proportionally or you'll hit truncation → JSON parse errors. The
classifier auto-retries on JSON parse failure by splitting the batch in half.

**5. Prompt caching matters.**
The system prompt is ~2000 tokens. With `cache_control: ephemeral`, every
call after the first reads it at 10× cheaper. Don't restructure
`classifier.SYSTEM_PROMPT` per call.

**6. r/financialindependence returns few "kept" posts.**
The community pins a lot of low-engagement scheduled threads. The
`MIN_UPVOTES` / `MIN_COMMENTS` filter (config.py) drops them, so you get
~10% of the listing.

**7. Idempotency is built in.**
- `collect-json` UPSERTs by Reddit fullname. Re-running doesn't dupe.
- `classify` skips rows where `classified_at IS NOT NULL`. To re-classify,
  `UPDATE posts SET classified_at = NULL` (or use the helper script).
- `build_report.py --upload --replace` overwrites the Drive file in place,
  preserving the share URL and file ID across runs.

**8. The synthetic seed is your best friend.**
`python seed_synthetic.py` inserts 10 hand-crafted posts covering the full
label space. Use it to smoke-test classifier changes without spending
API on real Reddit data.

---

## 📁 Project layout

```
reddit-finance-research/
├── cli.py                Entry point. Subcommands: setup, collect, collect-json, classify
├── config.py             Seeds, keyword patterns, regex demographics, model defaults
├── storage.py            SQLite schema, idempotent UPSERTs, migrations
├── collector.py          PRAW collector (Reddit script app required)
├── collector_json.py     Public JSON endpoints collector (no auth)
├── classifier.py         Regex demo + Claude batch classification (the heavy lifting)
├── build_report.py       18-sheet Excel report builder
├── drive_upload.py       Push xlsx to Google Drive via rclone CLI
├── seed_synthetic.py     10 synthetic posts for smoke testing
├── .env.example          Template — copy to .env and fill in keys
├── requirements.txt
└── data/                 (gitignored) reddit.db lives here
└── reports/              (gitignored) generated xlsx files
```

---

## 🌱 Seed subreddits (configurable in `config.SEEDS`)

Three layers, totalling 18 communities:

- **A — newbies / money fear**: r/personalfinance (21M), r/povertyfinance (2.7M),
  r/MiddleClassFinance, r/StudentLoans, r/Frugal (6.7M), r/FinancialPlanning, r/Money
- **B — learning to invest**: r/Bogleheads, r/investing (3.4M), r/dividends, r/ETFs,
  r/Fire, r/leanfire, r/coastFIRE, r/eupersonalfinance
- **C — psychology / habits**: r/DebtFree, r/ynab, r/financialindependence (2.4M)

Top 8 by subscribers in practice: personalfinance, Frugal, investing,
povertyfinance, financialindependence, DebtFree, eupersonalfinance,
FinancialPlanning. Skip eupersonalfinance if you only care about US.

---

## 🛡 For Reddit API reviewers

**This is a personal-use research script, not a service for other Redditors.**

| Aspect | Detail |
|---|---|
| Operator | Single individual, personal use |
| Reddit interaction | **Read-only** via official PRAW client or public JSON endpoints. No posting, no voting, no commenting, no messaging. |
| Data scope | Public posts and comments only, from subreddits listed in `config.SEEDS`. |
| Storage | Local SQLite file on the operator's machine. |
| Data sharing | **None.** Nothing is redistributed, resold, or shared externally. |
| User profiles | Public usernames are stored only for attribution of the post/comment they wrote. No profile scraping beyond that. |
| NSFW / private | Not collected. No DMs, no private subreddits. |
| Volume | Modest — ~100-500 requests/day at most, run manually on demand. |
| Cadence | Manual runs only, not a continuous service or bot. |

---

## 🏗 Implementation status

- [x] **Stage 1** — Collector (PRAW + JSON), SQLite storage, CLI scaffold
- [x] **Stage 2** — Claude classifier with regex demographics, expanded UA schema (18 fields)
- [x] **Stage 2.5** — Excel report builder (18 sheets) + Google Drive upload
- [ ] **Stage 3** — Embeddings + HDBSCAN clustering for auto-discovered themes
- [ ] **Stage 4** — Standalone analyser CLI (top_pains, verbatims, trends commands)
- [ ] **Stage 5** — Related-subreddit discovery via mention graph
- [ ] **Stage 6** — Streamlit dashboard

---

## 📝 License

Personal research code. No Reddit data is included in this repo. Use at
your own discretion; respect Reddit's API terms.
