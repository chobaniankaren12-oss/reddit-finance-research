"""Post classifier — regex demographics + Claude LLM pain/goal/gap labels.

Pipeline per unclassified post:
    1. Regex pass extracts age / gender / income / country into the
       `demographics` table (cheap, runs on all posts).
    2. If `config.has_signal(title+selftext)` fires → batch the post into a
       Claude call to label pain_category / goal_category / knowledge_gap /
       emotion / life_stage / quote_worthy.
    3. If no signal → mark with pain=goal='none' so re-runs skip the row.

All writes stamp posts.classified_at, so the orchestrator is idempotent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

from anthropic import Anthropic
from tqdm import tqdm

from config import (
    AGE_GENDER_RE,
    AGE_ONLY_RE,
    ANTHROPIC_API_KEY,
    CLASSIFY_BATCH_SIZE,
    CLAUDE_MODEL,
    COUNTRY_RE,
    INCOME_RE,
    has_signal,
)
from storage import (
    count_unclassified_comments,
    count_unclassified_posts,
    get_conn,
    get_unclassified_comments,
    get_unclassified_posts,
    update_comment_classification,
    update_post_classification,
    upsert_demographic,
)

logger = logging.getLogger(__name__)


# --- Allowed label vocabularies (mirrored in SYSTEM_PROMPT) -----------------
# US-focused — designed for EdTech user-acquisition research on Reddit.

PAIN_CATEGORIES: tuple[str, ...] = (
    "debt",
    "no_savings",
    "fear_of_investing",
    "lifestyle_inflation",
    "retirement_anxiety",
    "financial_illiteracy",
    "family_pressure",
    "income_low",
    "housing_stress",
    "healthcare_cost",
    "job_insecurity",                # worried about layoff, gig instability
    "uncertain_future",              # economy/AI/recession anxiety
    "regret_past_decisions",         # "wish I'd started earlier"
    "relationship_money_tension",    # spouse/family money conflict
    "tax_anxiety",
    "credit_score_anxiety",
    "other",
    "none",
)

GOAL_CATEGORIES: tuple[str, ...] = (
    "start_investing",
    "build_emergency_fund",
    "pay_off_debt",
    "retire_early",
    "buy_house",
    "increase_income",
    "learn_basics",
    "budgeting",
    "save_for_kids",
    "financial_independence",        # FI math (distinct from retire_early ethos)
    "leave_inheritance",
    "career_change",
    "build_credit_score",
    "tax_optimization",
    "build_side_income",
    "other",
    "none",
)

EMOTIONS: tuple[str, ...] = (
    "anxious", "frustrated", "hopeful", "ashamed", "confused",
    "motivated", "determined", "overwhelmed", "regretful", "guilty",
    "envious", "relieved", "proud", "fearful", "neutral",
)

LIFE_STAGES: tuple[str, ...] = (
    "student", "early_career", "mid_career", "parent",
    "near_retirement", "retired", "unknown",
)

FINANCIAL_EXPERIENCE: tuple[str, ...] = (
    "beginner", "intermediate", "advanced", "unknown",
)

EMPLOYMENT_STATUS: tuple[str, ...] = (
    "W2_employee",            # traditional salaried/hourly
    "contractor_or_gig",      # 1099, freelance, Uber/DoorDash
    "self_employed",          # owns their business
    "unemployed",
    "retired",
    "student",
    "government_or_military",
    "unknown",
)

FAMILY_STATUS: tuple[str, ...] = (
    "single",
    "partnered_unmarried",
    "married_no_kids",
    "married_with_kids",
    "single_parent",
    "divorced",
    "caregiver_aging_parent",
    "unknown",
)

TRIGGER_EVENTS: tuple[str, ...] = (
    "job_loss",
    "job_change",
    "got_raise_or_bonus",
    "new_baby",
    "marriage",
    "divorce",
    "death_in_family",
    "inheritance",
    "home_purchase",
    "home_sale",
    "relocation",
    "kid_starting_college",
    "approaching_retirement",
    "just_retired",
    "medical_event",
    "debt_paid_off_milestone",
    "investment_gain_or_loss",
    "started_side_hustle",
    "none",
    "unknown",
)

BUYING_SIGNALS: tuple[str, ...] = (
    "actively_looking_for_resource",   # "recommend a book/course/app"
    "asking_for_advisor",              # wants a human advisor/CFP
    "paying_for_solution_now",         # mentions current advisor/course/app
    "research_phase",                  # learning, no buying urgency
    "no_buying_signal",
    "unknown",
)

URGENCY: tuple[str, ...] = (
    "immediate_crisis",       # bills due now, eviction, can't afford essentials
    "short_term_planning",    # next 1-12 months
    "long_term_planning",     # 5+ years out
    "just_exploring",         # casual interest
    "unknown",
)

# Multi-value tag list for the `interests` field. Output as JSON array.
INTERESTS_TAGS: tuple[str, ...] = (
    "401k", "roth_ira", "traditional_ira", "hsa", "fsa", "529_plan",
    "stocks", "bonds", "etfs", "mutual_funds", "index_funds", "dividends",
    "real_estate", "REITs", "rental_property",
    "crypto",
    "FIRE_movement", "side_hustle", "gig_work",
    "frugal_living", "debt_payoff", "budgeting_apps",
    "credit_score", "credit_cards", "mortgage", "refinancing", "HELOC",
    "student_loans",
    "taxes", "tax_optimization",
    "social_security", "medicare", "pension",
    "health_insurance", "life_insurance",
    "career_change", "salary_negotiation",
)

# Hint list shown to the LLM so it normalizes common brand names. Output as
# a JSON array of strings — exact names with underscores.
COMMON_BRANDS_HINT = [
    # Personalities
    "Dave_Ramsey", "Suze_Orman", "Caleb_Hammer", "Money_Guy",
    "Mr_Money_Mustache", "Ramit_Sethi", "Bogleheads_community",
    # Brokerages
    "Vanguard", "Fidelity", "Schwab", "Robinhood", "Webull", "E_Trade",
    # Budget apps
    "YNAB", "Mint", "Rocket_Money", "Monarch", "Personal_Capital", "Empower",
    # Robo-advisors
    "Betterment", "Wealthfront", "Acorns",
    # Crypto
    "Coinbase",
    # Media
    "NerdWallet", "Bankrate", "Investopedia", "WSJ", "Bloomberg",
]

_PAIN_SET = frozenset(PAIN_CATEGORIES)
_GOAL_SET = frozenset(GOAL_CATEGORIES)
_EMO_SET = frozenset(EMOTIONS)
_STAGE_SET = frozenset(LIFE_STAGES)
_EXP_SET = frozenset(FINANCIAL_EXPERIENCE)
_EMP_SET = frozenset(EMPLOYMENT_STATUS)
_FAM_SET = frozenset(FAMILY_STATUS)
_TRIG_SET = frozenset(TRIGGER_EVENTS)
_BUY_SET = frozenset(BUYING_SIGNALS)
_URG_SET = frozenset(URGENCY)
_INTEREST_SET = frozenset(INTERESTS_TAGS)

DEFAULT_NONE: dict[str, Any] = {
    "pain_category": "none",
    "pain_other": None,
    "goal_category": "none",
    "goal_other": None,
    "knowledge_gap": None,
    "emotion": "neutral",
    "life_stage": "unknown",
    "financial_experience": "unknown",
    "employment_status": "unknown",
    "family_status": "unknown",
    "intent": None,
    "interests": None,
    "trigger_event": "unknown",
    "buying_signal": "unknown",
    "urgency": "unknown",
    "mentioned_brands": None,
    "quote_worthy": 0,
}


SYSTEM_PROMPT = (
    "You are analyzing Reddit posts and comments from US personal-finance "
    "communities for an EdTech research project focused on USER ACQUISITION "
    "in social media. Output is consumed by software — strict JSON only.\n\n"
    "For each item in the user's batch return one JSON object with the "
    "fields below. Use EXACT enum strings — no synonyms, no plurals. Use "
    "\"unknown\" or \"none\" liberally rather than guessing. We focus on "
    "US users; ignore non-US-specific products.\n\n"

    "===== CORE LABELS =====\n"
    "- \"id\" (string): echo back the input id.\n"
    "- \"pain_category\" (string): one of {pain}.\n"
    "- \"pain_other\" (string|null): ≤8 words, only when pain_category==\"other\".\n"
    "- \"goal_category\" (string): one of {goal}.\n"
    "- \"goal_other\" (string|null): ≤8 words, only when goal_category==\"other\".\n"
    "- \"knowledge_gap\" (string|null): ≤12 words describing what they don't understand. null when no clear gap.\n"
    "- \"emotion\" (string): one of {emo}.\n"
    "- \"quote_worthy\" (boolean): true if the text contains a clear, "
    "first-person, quotable expression usable in marketing copy.\n\n"

    "===== USER PROFILE =====\n"
    "- \"life_stage\" (string): one of {stage}.\n"
    "- \"financial_experience\" (string): one of {exp}. "
    "Judge by vocabulary and concept complexity:\n"
    "  • beginner: asks about basics, doesn't know account types\n"
    "  • intermediate: has accounts, refining strategy, asks comparison questions\n"
    "  • advanced: discusses tax optimization, FIRE math, complex products\n"
    "- \"employment_status\" (string): one of {emp}.\n"
    "  W2_employee = traditional salaried/hourly; contractor_or_gig = 1099/Uber/freelance.\n"
    "- \"family_status\" (string): one of {fam}.\n\n"

    "===== INTENT & INTERESTS =====\n"
    "- \"intent\" (string|null): ≤8 words. The immediate next ACTION they're "
    "considering (different from long-term goal). Examples:\n"
    "  • \"open Roth IRA\"\n"
    "  • \"consolidate credit card debt\"\n"
    "  • \"switch jobs for higher pay\"\n"
    "  • \"buy first index fund\"\n"
    "  null if no clear intent.\n"
    "- \"interests\" (array of strings): subset of {interests}. "
    "Empty array [] if none clear.\n\n"

    "===== USER-ACQUISITION SIGNALS (most important for our research) =====\n"
    "- \"trigger_event\" (string): one of {trig}. The life event motivating this post.\n"
    "- \"buying_signal\" (string): one of {buy}.\n"
    "  • actively_looking_for_resource: explicitly asks for a book/course/app\n"
    "  • asking_for_advisor: wants a human advisor/CFP\n"
    "  • paying_for_solution_now: mentions their current advisor/course/app\n"
    "  • research_phase: learning, no buying urgency\n"
    "  • no_buying_signal: pure venting/sharing without seeking solutions\n"
    "- \"urgency\" (string): one of {urg}.\n"
    "- \"mentioned_brands\" (array of strings): US finance brands/apps/personalities mentioned. "
    "Use exact underscored names from this hint list when applicable: "
    "{brands}. Free-text is fine for less common ones. Empty array [] if none.\n\n"

    "===== RULES =====\n"
    "- Output ONLY a JSON array of objects. No prose, no markdown fences, no trailing commas.\n"
    "- Strict enum match. Use \"unknown\" / \"none\" / null / [] freely — DO NOT guess.\n"
    "- \"interests\" and \"mentioned_brands\" are JSON arrays of strings (not comma-separated).\n"
).format(
    pain=list(PAIN_CATEGORIES),
    goal=list(GOAL_CATEGORIES),
    emo=list(EMOTIONS),
    stage=list(LIFE_STAGES),
    exp=list(FINANCIAL_EXPERIENCE),
    emp=list(EMPLOYMENT_STATUS),
    fam=list(FAMILY_STATUS),
    trig=list(TRIGGER_EVENTS),
    buy=list(BUYING_SIGNALS),
    urg=list(URGENCY),
    interests=list(INTERESTS_TAGS),
    brands=COMMON_BRANDS_HINT,
)


# --- Regex demographics -----------------------------------------------------


def parse_demographics(text: str | None) -> dict[str, Any]:
    """Best-effort regex extraction. None for any field not found."""
    out: dict[str, Any] = {"age": None, "gender": None, "income": None, "country": None}
    if not text:
        return out

    m = AGE_GENDER_RE.search(text)
    if m:
        try:
            out["age"] = int(m.group(1))
            out["gender"] = m.group(2).upper()
        except (TypeError, ValueError):
            pass
    if out["age"] is None:
        m = AGE_ONLY_RE.search(text)
        if m:
            for g in m.groups():
                if g:
                    try:
                        out["age"] = int(g)
                        break
                    except ValueError:
                        pass

    m = INCOME_RE.search(text)
    if m:
        try:
            if m.group("k"):
                out["income"] = str(int(m.group("k")) * 1000)
            elif m.group("thou") and m.group("thou_rest"):
                out["income"] = str(int(m.group("thou")) * 1000 + int(m.group("thou_rest")))
            elif m.group("full"):
                out["income"] = str(int(m.group("full")))
        except (TypeError, ValueError):
            pass

    m = COUNTRY_RE.search(text)
    if m:
        out["country"] = m.group(1)
    return out


# --- Claude wiring ----------------------------------------------------------


class ClassificationError(RuntimeError):
    """Raised when a Claude response can't be turned into rows."""


def _build_user_message(batch: list[dict[str, Any]]) -> str:
    payload = []
    for row in batch:
        title = (row.get("title") or "").strip()
        body = (row.get("selftext") or "").strip()
        # Long posts blow up tokens for marginal extra signal.
        if len(body) > 1500:
            body = body[:1500] + "…"
        payload.append({"id": row["id"], "title": title, "body": body})
    return "Posts to classify:\n" + json.dumps(payload, ensure_ascii=False)


def _normalise_result(raw: dict[str, Any]) -> dict[str, Any]:
    def pick(value: Any, allowed: frozenset[str], default: str) -> str:
        return value if isinstance(value, str) and value in allowed else default

    def join_tags(value: Any, allowed: frozenset[str] | None) -> str | None:
        """Comma-join a JSON array of tags. Filter against `allowed` if given.
        Falsy → None so SQL stores NULL."""
        if not value:
            return None
        if isinstance(value, str):  # forgiving: accept "a, b" too
            tags = [t.strip() for t in value.split(",") if t.strip()]
        elif isinstance(value, list):
            tags = [str(t).strip() for t in value if str(t).strip()]
        else:
            return None
        if allowed is not None:
            tags = [t for t in tags if t in allowed]
        return ",".join(dict.fromkeys(tags)) if tags else None  # dedup, preserve order

    def free_join(value: Any) -> str | None:
        """For mentioned_brands — free-text array, no whitelist."""
        if not value:
            return None
        if isinstance(value, str):
            tags = [t.strip() for t in value.split(",") if t.strip()]
        elif isinstance(value, list):
            tags = [str(t).strip().replace(" ", "_") for t in value if str(t).strip()]
        else:
            return None
        return ",".join(dict.fromkeys(tags)) if tags else None

    pain = pick(raw.get("pain_category"), _PAIN_SET, "none")
    goal = pick(raw.get("goal_category"), _GOAL_SET, "none")

    return {
        # Core labels
        "pain_category": pain,
        "pain_other": (raw.get("pain_other") or None) if pain == "other" else None,
        "goal_category": goal,
        "goal_other": (raw.get("goal_other") or None) if goal == "other" else None,
        "knowledge_gap": raw.get("knowledge_gap") or None,
        "emotion": pick(raw.get("emotion"), _EMO_SET, "neutral"),
        "quote_worthy": 1 if bool(raw.get("quote_worthy")) else 0,
        # User profile
        "life_stage": pick(raw.get("life_stage"), _STAGE_SET, "unknown"),
        "financial_experience": pick(raw.get("financial_experience"), _EXP_SET, "unknown"),
        "employment_status": pick(raw.get("employment_status"), _EMP_SET, "unknown"),
        "family_status": pick(raw.get("family_status"), _FAM_SET, "unknown"),
        # Intent & interests
        "intent": raw.get("intent") or None,
        "interests": join_tags(raw.get("interests"), _INTEREST_SET),
        # UA signals
        "trigger_event": pick(raw.get("trigger_event"), _TRIG_SET, "unknown"),
        "buying_signal": pick(raw.get("buying_signal"), _BUY_SET, "unknown"),
        "urgency": pick(raw.get("urgency"), _URG_SET, "unknown"),
        "mentioned_brands": free_join(raw.get("mentioned_brands")),
    }


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if text.startswith("["):
        return json.loads(text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("no JSON array found", text, 0)
    return json.loads(text[start : end + 1])


def _call_claude(
    client: Anthropic, model: str, batch: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """One Claude call. Returns post_id → normalised classification."""
    user_msg = _build_user_message(batch)
    response = client.messages.create(
        model=model,
        max_tokens=8192,  # expanded schema → ~250 tokens output per item
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    text_parts = [
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ]
    raw_text = "".join(text_parts)
    try:
        parsed = _extract_json_array(raw_text)
    except json.JSONDecodeError as exc:
        raise ClassificationError(
            f"could not parse JSON: {exc}; head={raw_text[:200]!r}"
        ) from exc

    out: dict[str, dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        post_id = item.get("id")
        if isinstance(post_id, str):
            out[post_id] = _normalise_result(item)
    return out


def _classify_with_retry(
    client: Anthropic,
    model: str,
    batch: list[dict[str, Any]],
    depth: int = 0,
) -> dict[str, dict[str, Any]]:
    """Call Claude; on failure, split batch in half and retry."""
    try:
        result = _call_claude(client, model, batch)
        missing = [r for r in batch if r["id"] not in result]
        if missing and len(missing) < len(batch):
            # Claude returned partial output — re-ask for the rest.
            retry = _classify_with_retry(client, model, missing, depth + 1)
            result.update(retry)
        elif missing and len(batch) > 1:
            # Every row missing AND batch > 1: treat as failure → split.
            raise ClassificationError(f"all {len(batch)} ids missing from response")
        return result
    except Exception as exc:  # noqa: BLE001
        if len(batch) == 1 or depth >= 3:
            logger.error(
                "Giving up on batch of %d at depth=%d: %s", len(batch), depth, exc
            )
            return {row["id"]: DEFAULT_NONE.copy() for row in batch}
        mid = len(batch) // 2
        logger.warning(
            "Batch of %d failed (%s) — splitting %d / %d",
            len(batch), exc, mid, len(batch) - mid,
        )
        left = _classify_with_retry(client, model, batch[:mid], depth + 1)
        right = _classify_with_retry(client, model, batch[mid:], depth + 1)
        return {**left, **right}


# --- Persistence ------------------------------------------------------------


def _persist_post(
    conn, row: dict[str, Any], fields: dict[str, Any], now_utc: int
) -> bool:
    """Write classification + demographics for one post. Returns True if a
    demographics row was written."""
    update_post_classification(conn, row["id"], fields, now_utc)

    text = f"{row.get('title') or ''}\n{row.get('selftext') or ''}"
    demo = parse_demographics(text)
    has_demo = any(v is not None for v in demo.values())
    stage = fields.get("life_stage")
    has_stage = isinstance(stage, str) and stage != "unknown"

    if not (has_demo or has_stage):
        return False

    upsert_demographic(
        conn,
        {
            "source_type": "post",
            "source_id": row["id"],
            "age": demo["age"],
            "gender": demo["gender"],
            "income": demo["income"],
            "country": demo["country"],
            "life_stage": stage if has_stage else None,
            "extracted_at": now_utc,
        },
    )
    return True


# --- Orchestration ----------------------------------------------------------


def _chunk(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _split_signal(
    rows: list[Any], skip_keyword_filter: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition raw DB rows into (signal, no_signal) plain-dict lists.

    With skip_keyword_filter=True everything is signal — useful when the
    keyword list is too narrow and you don't want to miss implicit pains.
    """
    sig: list[dict[str, Any]] = []
    no_sig: list[dict[str, Any]] = []
    for r in rows:
        item = {
            "id": r["id"],
            "title": r["title"] or "",
            "selftext": r["selftext"] or "",
        }
        if skip_keyword_filter:
            sig.append(item)
            continue
        text = f"{item['title']}\n{item['selftext']}"
        (sig if has_signal(text) else no_sig).append(item)
    return sig, no_sig


def _split_signal_comments(
    rows: list[Any], skip_keyword_filter: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Same idea as _split_signal but for the comments table.

    Comments don't have a 'title'; we pack the body into both title and
    selftext slots so the existing batch-building code works unchanged.
    """
    sig: list[dict[str, Any]] = []
    no_sig: list[dict[str, Any]] = []
    for r in rows:
        body = r["body"] or ""
        item = {
            "id": r["id"],
            "title": body[:120],
            "selftext": body,
            "_kind": "comment",
        }
        if skip_keyword_filter:
            sig.append(item)
            continue
        (sig if has_signal(body) else no_sig).append(item)
    return sig, no_sig


def _persist_comment(
    conn, row: dict[str, Any], fields: dict[str, Any], now_utc: int
) -> bool:
    """Write classification + demographics for one comment."""
    update_comment_classification(conn, row["id"], fields, now_utc)

    demo = parse_demographics(row.get("selftext") or "")
    has_demo = any(v is not None for v in demo.values())
    stage = fields.get("life_stage")
    has_stage = isinstance(stage, str) and stage != "unknown"

    if not (has_demo or has_stage):
        return False

    upsert_demographic(
        conn,
        {
            "source_type": "comment",
            "source_id": row["id"],
            "age": demo["age"],
            "gender": demo["gender"],
            "income": demo["income"],
            "country": demo["country"],
            "life_stage": stage if has_stage else None,
            "extracted_at": now_utc,
        },
    )
    return True


def run_comments_classifier(
    batch_size: int = 20,  # smaller default — expanded prompt → more output tokens
    limit: int | None = None,
    model: str = CLAUDE_MODEL,
    skip_keyword_filter: bool = False,
) -> dict[str, int]:
    """Classify unclassified comments. Mirrors run_classifier."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env first.")

    with get_conn() as conn:
        total_pending = count_unclassified_comments(conn)
        rows = get_unclassified_comments(conn, limit if limit else total_pending)

    counters = {"signal": 0, "no_signal": 0, "demographics": 0, "errors": 0}
    if not rows:
        logger.info("Nothing to classify in comments (0 pending).")
        return counters

    signal_rows, no_signal_rows = _split_signal_comments(
        rows, skip_keyword_filter=skip_keyword_filter
    )
    logger.info(
        "Comments pending=%d, picked=%d (signal=%d, no_signal=%d), model=%s",
        total_pending, len(rows), len(signal_rows), len(no_signal_rows), model,
    )

    if no_signal_rows:
        with get_conn() as conn:
            now = int(time.time())
            for row in tqdm(no_signal_rows, desc="no-sig comments", unit="cmt", leave=False):
                if _persist_comment(conn, row, DEFAULT_NONE, now):
                    counters["demographics"] += 1
                counters["no_signal"] += 1

    if not signal_rows:
        logger.info("comments classify done — %s", counters)
        return counters

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    batches = list(_chunk(signal_rows, batch_size))
    for batch in tqdm(batches, desc=f"claude-comments/{model}", unit="batch"):
        try:
            results = _classify_with_retry(client, model, batch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Comments batch failed: %s", exc)
            counters["errors"] += 1
            continue
        with get_conn() as conn:
            now = int(time.time())
            for row in batch:
                fields = results.get(row["id"], DEFAULT_NONE.copy())
                if _persist_comment(conn, row, fields, now):
                    counters["demographics"] += 1
                counters["signal"] += 1

    logger.info("comments classify done — %s", counters)
    return counters


def run_classifier(
    batch_size: int = CLASSIFY_BATCH_SIZE,
    limit: int | None = None,
    model: str = CLAUDE_MODEL,
    skip_keyword_filter: bool = False,
) -> dict[str, int]:
    """Classify unclassified posts. Returns counters.

    skip_keyword_filter=True bypasses the cheap regex pre-filter and sends
    every post to Claude. Use when the keyword list is too narrow (common —
    real pain expressions are more diverse than 33 regex patterns) and you
    don't want to miss implicit signal.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env first.")

    with get_conn() as conn:
        total_pending = count_unclassified_posts(conn)
        rows = get_unclassified_posts(conn, limit if limit else total_pending)

    counters = {"signal": 0, "no_signal": 0, "demographics": 0, "errors": 0}
    if not rows:
        logger.info("Nothing to classify (0 unclassified posts).")
        return counters

    signal_rows, no_signal_rows = _split_signal(
        rows, skip_keyword_filter=skip_keyword_filter
    )
    logger.info(
        "Pending=%d, picked=%d (signal=%d, no_signal=%d), model=%s, batch_size=%d, skip_filter=%s",
        total_pending, len(rows), len(signal_rows), len(no_signal_rows),
        model, batch_size, skip_keyword_filter,
    )

    # 1) No-signal fast path: default labels, no Claude call.
    if no_signal_rows:
        with get_conn() as conn:
            now = int(time.time())
            for row in tqdm(no_signal_rows, desc="no-signal", unit="post", leave=False):
                if _persist_post(conn, row, DEFAULT_NONE, now):
                    counters["demographics"] += 1
                counters["no_signal"] += 1

    if not signal_rows:
        logger.info("classify done — %s", counters)
        return counters

    # 2) Signal posts → Claude.
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    batches = list(_chunk(signal_rows, batch_size))
    for batch in tqdm(batches, desc=f"claude/{model}", unit="batch"):
        try:
            results = _classify_with_retry(client, model, batch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled batch failure: %s", exc)
            counters["errors"] += 1
            continue
        with get_conn() as conn:
            now = int(time.time())
            for row in batch:
                fields = results.get(row["id"], DEFAULT_NONE.copy())
                if _persist_post(conn, row, fields, now):
                    counters["demographics"] += 1
                counters["signal"] += 1

    logger.info("classify done — %s", counters)
    return counters
