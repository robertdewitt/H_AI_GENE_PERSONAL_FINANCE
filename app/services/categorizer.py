"""Auto-categorize transactions using:
1. Learned rules (user corrections stored in category_rules table)
2. Keyword heuristics (fast, no external dependency)
3. Local LLM via Ollama (free, private — runs on your machine)

The system learns: when a user corrects a category, the description
pattern is saved as a rule. Next time, the rule fires before the LLM.
"""
import json
import logging
import re

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.category import Category
from app.models.category_rule import CategoryRule
from app.models.transaction import Transaction

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"

KEYWORD_MAP = {
    "groceries": [
        "grocery", "whole foods", "trader joe", "kroger", "safeway",
        "aldi", "costco", "walmart supercenter", "publix", "heb",
        # UK
        "tesco", "sainsbury", "asda", "waitrose", "morrisons", "ocado",
        "marks & spencer food", "m&s food", "lidl", "co-op food",
        "iceland food", "budgens", "spar ",
    ],
    "dining out": [
        "restaurant", "mcdonald", "starbucks", "chipotle", "subway",
        "doordash", "grubhub", "uber eats", "pizza", "cafe", "diner",
        "burger", "taco", "sushi", "thai", "panda express",
        # UK
        "deliveroo", "just eat", "wingstop", "nandos", "nando's",
        "wagamama", "itsu", "wasabi", "pret a manger", "pret ",
        "greggs", "costa coffee", "caffe nero", "leon ", "dishoom",
        "cko*", "dojo*", "square*",  # UK payment processor prefixes
    ],
    "gas / fuel": [
        "shell", "exxon", "chevron", "bp ", "gas station", "fuel",
        "mobil", "speedway", "wawa gas", "circle k",
        # UK
        "texaco", "esso", "totalenergies", "jet petrol",
    ],
    "transportation": [
        "uber", "lyft", "taxi", "transit", "metro", "parking",
        "toll", "ez pass",
        # UK
        "freenow", "free now", "bolt ride", "addison lee",
        "tfl ", "transport for london", "trainline", "avanti",
        "great western", "southeastern", "thameslink", "greater anglia",
        "c2c train", "national rail", "oyster", "contactless tfl",
        "national express", "megabus", "flixbus",
        "apcoa", "q-park", "ncp parking", "justpark",
    ],
    "utilities": [
        "electric", "water bill", "gas bill", "utility", "power",
        "sewage", "garbage", "waste management", "xcel", "pge",
        "comcast", "at&t", "verizon", "t-mobile", "internet",
        # UK
        "british gas", "octopus energy", "eon ", "e.on", "edf energy",
        "bulb energy", "ovo energy", "scottish power", "npower",
        "thames water", "anglian water", "severn trent",
        "bt internet", "bt group", "sky broadband", "virgin media",
        "talktalk", "ee broadband", "vodafone home",
        "council tax", "water rates",
    ],
    "subscriptions": [
        "netflix", "spotify", "hulu", "disney+", "hbo",
        "apple music", "youtube premium", "amazon prime",
        "adobe", "microsoft 365", "icloud",
        # UK / global
        "apple.com/bill", "google one", "dropbox", "github",
        "notion ", "chatgpt", "openai", "anthropic",
        "times subscription", "guardian", "financial times",
    ],
    "insurance": [
        "insurance", "geico", "state farm", "allstate", "progressive",
        "liberty mutual", "usaa",
        # UK
        "aviva", "axa", "zurich", "lloyds insurance", "admiral ",
        "direct line", "churchill", "hastings direct", "comparethemarket",
        "legal & general", "prudential", "standard life",
    ],
    "healthcare": [
        "pharmacy", "cvs", "walgreens", "doctor", "hospital",
        "medical", "dental", "optometrist", "urgent care",
        "labcorp", "quest diag",
        # UK
        "boots pharmacy", "lloyds pharmacy", "superdrug",
        "nhs ", "bupa", "vitality health", "axa health",
        "vision express", "specsavers", "optical",
    ],
    "shopping": [
        "amazon", "target", "best buy", "home depot", "lowes",
        "ikea", "ebay", "etsy", "nordstrom", "macys",
        # UK
        "john lewis", "argos", "currys", "next ", "primark",
        "h&m ", "zara ", "topshop", "asos", "very.co",
        "b&q", "screwfix", "toolstation", "halfords",
        "marks & spencer", "m&s ",
    ],
    "rent / mortgage": ["rent", "mortgage", "hoa ", "lease", "ground rent", "service charge"],
    "entertainment": [
        "cinema", "movie", "theater", "concert", "ticket",
        "amc ", "regal", "event",
        # UK
        "odeon", "vue cinema", "cineworld", "picturehouse",
        "ticketmaster", "seetickets", "eventbrite",
        "sky sports", "now tv", "dazn",
    ],
    "travel": [
        "airline", "hotel", "airbnb", "booking.com", "expedia",
        "delta", "united", "american air", "southwest", "marriott",
        "hilton",
        # UK / global
        "british airways", "easyjet", "ryanair", "jet2", "tui ",
        "virgin atlantic", "emirates", "qatar airways",
        "premier inn", "travelodge", "ibis hotel",
        "hotels.com", "trivago", "kayak", "skyscanner",
    ],
    "salary": ["payroll", "direct deposit", "salary", "wage", "faster payment", "bacs credit"],
    "interest": ["interest earned", "interest payment", "apy", "dividend"],
    "interest expense": ["interest charged", "overdraft interest", "finance charge", "overdraft fee"],
    "account transfer": [
        "transfer", "xfer", "ach", "wire", "zelle",
        "venmo", "paypal", "payment thank you",
        "online payment", "automatic payment",
        "autopay", "payment received", "payment from",
        # UK
        "monzo", "revolut", "wise transfer", "starling",
        "faster payments", "standing order", "direct debit",
    ],
    "fees & charges": [
        "late payment fee", "late fee", "overdraft fee", "annual fee",
        "foreign transaction", "atm fee", "service fee", "charge ",
        "penalty", "nsf fee", "returned item",
    ],
}


# Amount-aware keyword rules: (pattern) → (positive_category, negative_category)
# Checked before KEYWORD_MAP when amount is known. None means "skip this direction".
SIGNED_KEYWORD_MAP: list[tuple[list[str], str | None, str | None]] = [
    # keywords, positive_cat (credit/inflow), negative_cat (debit/outflow)
    (["interest interest", "savings interest", "deposit interest",
      "credit interest", "interest credit"],       "interest",          "interest expense"),
    (["interest earned", "interest received",
      "interest paid to you"],                     "interest",          "interest expense"),
    (["interest charged", "overdraft interest",
      "finance charge"],                           None,                "interest expense"),
    (["interest payment"],                         "interest",          "interest expense"),
    # Generic catch-all — must come last so specific entries above win
    (["interest"],                                 "interest",          "interest expense"),
    (["dividend"],                                 "interest",          None),
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def match_keyword(description: str, amount: float | None = None) -> str | None:
    """Fast keyword-based category guess.

    When *amount* is provided, amount-aware rules are checked first so that
    e.g. interest income vs interest expense can be distinguished by sign.
    """
    desc = _normalize(description)

    if amount is not None:
        for keywords, pos_cat, neg_cat in SIGNED_KEYWORD_MAP:
            for kw in keywords:
                if kw in desc:
                    result = pos_cat if amount >= 0 else neg_cat
                    if result is not None:
                        return result
                    # matched but no category for this direction — fall through
                    break

    for category_name, keywords in KEYWORD_MAP.items():
        for kw in keywords:
            if kw in desc:
                return category_name
    return None


def match_learned_rule(db: Session, description: str) -> int | None:
    """Check stored rules from previous user corrections.

    Matching uses two strategies in priority order:
    1. Exact substring match (original behaviour — fast, zero false-positives)
    2. Token-overlap match: ≥75 % of the rule's meaningful tokens must appear
       in the incoming description.  This handles descriptions that share a
       payee name but differ in transaction reference codes, e.g.
         rule pattern : "electronic payment mohammed khamal udd maya tutor"
         incoming desc: "electronic payment ipb2603313170069 mohammed khamal udd"
    """
    desc = _normalize(description)
    # Pre-extract tokens from the incoming description once
    desc_tokens = set(_pattern_tokens(desc))

    rules = db.execute(
        select(CategoryRule).order_by(CategoryRule.hit_count.desc())
    ).scalars().all()

    for rule in rules:
        pattern_lower = (rule.pattern or "").lower().strip()

        # An empty pattern is a substring of *every* description, so a single
        # blank rule would silently swallow the entire ledger into one
        # category. Never let one match.
        if not pattern_lower:
            continue

        # Strategy 1: fast exact substring
        if pattern_lower in desc:
            rule.hit_count += 1
            db.commit()
            return rule.category_id

        # Strategy 2: token overlap (≥75 % of rule tokens found in desc tokens)
        rule_tokens = _pattern_tokens(pattern_lower)
        if len(rule_tokens) >= 2:
            overlap = sum(1 for t in rule_tokens if t in desc_tokens)
            if overlap / len(rule_tokens) >= 0.75:
                rule.hit_count += 1
                db.commit()
                return rule.category_id

    return None


def learn_from_correction(
    db: Session,
    description: str,
    category_id: int,
    user_id: int | None = None,
    apply_to_history: bool = True,
) -> tuple[CategoryRule, int]:
    """Store a user's category correction as a learned rule and, when
    ``apply_to_history`` is True, retroactively update every existing
    transaction whose description matches.

    Uses two strategies for retroactive updates:
    1. Exact match on the original description (case-insensitive)
    2. Fuzzy match via the extracted pattern (handles whitespace differences)

    Set ``apply_to_history=False`` to scope the change to *this one
    transaction* — the rule still updates so future imports route to the
    new category, but no other rows are touched. That's the right
    default for the single-transaction edit form, where the user often
    only wants to correct the row in front of them. Bulk-categorise and
    explicit "apply rule to history" actions keep the default of True.

    The rule is attributed to ``user_id`` so each user gets their own
    independent rule set. When not supplied we fall back to the category's
    owning user — categories are per-user, so this is unambiguous.

    Returns (rule, count_updated).
    """
    pattern = (_extract_pattern(description) or "").strip()
    if not pattern:
        # Nothing distinctive survived extraction (e.g. a description that is
        # all digits or punctuation). Storing a blank pattern would create a
        # catch-all rule matching every future transaction, so learn nothing.
        log.warning(
            "categorizer: refusing to learn a blank rule from description %r",
            description,
        )
        return None, 0

    if user_id is None:
        # Derive from the category we're attaching the rule to so a missing
        # user_id never silently creates a NULL-owned rule again.
        from app.models.category import Category as _Cat
        cat = db.get(_Cat, category_id)
        user_id = cat.user_id if cat is not None else None

    # A user's correction is authoritative for the pattern — one pattern
    # maps to one category, full stop. Look up *every* rule for this
    # (pattern, user) regardless of which category it currently points
    # at, re-point the most useful one to the new category, and delete
    # any leftover duplicates. The previous behaviour filtered the
    # lookup by the new category_id, which meant a pre-existing rule
    # pointing to a *wrong* category was never updated — just
    # shadow-duplicated by a new correctly-pointed row, with the old
    # row continuing to win on future imports (FREENOW bug).
    existing_rules = db.execute(
        select(CategoryRule).where(
            CategoryRule.pattern == pattern,
            CategoryRule.user_id == user_id,
        ).order_by(CategoryRule.hit_count.desc(), CategoryRule.id.asc())
    ).scalars().all()

    if existing_rules:
        # Preserve the strongest rule's hit_count history; re-point it
        # at the new category, then delete any siblings.
        existing = existing_rules[0]
        existing.category_id = category_id
        existing.source = "user_correction"
        existing.hit_count = (existing.hit_count or 0) + 1
        for stale in existing_rules[1:]:
            db.delete(stale)
    else:
        existing = CategoryRule(
            pattern=pattern,
            category_id=category_id,
            user_id=user_id,
            source="user_correction",
        )
        db.add(existing)
    db.flush()

    count = 0
    if apply_to_history:
        count = _apply_to_matching_transactions(db, description, pattern, category_id)

    return existing, count


def _apply_to_matching_transactions(
    db: Session,
    original_description: str,
    pattern: str,
    category_id: int,
) -> int:
    """Update category on all transactions that match the description.

    First does an exact case-insensitive match on the full description,
    then also catches fuzzy matches via the extracted pattern (with
    whitespace collapsed in both sides of the comparison).
    """
    already_updated: set[int] = set()

    from sqlalchemy import or_

    not_already_set = or_(
        Transaction.category_id.is_(None),
        Transaction.category_id != category_id,
    )

    exact_lower = original_description.strip().lower()
    exact_matches = db.execute(
        select(Transaction).where(
            func.lower(Transaction.description) == exact_lower,
            not_already_set,
        )
    ).scalars().all()

    for txn in exact_matches:
        txn.category_id = category_id
        already_updated.add(txn.id)

    like_pattern = f"%{pattern}%"
    fuzzy_matches = db.execute(
        select(Transaction).where(
            func.replace(
                func.replace(
                    func.lower(Transaction.description), "  ", " "
                ),
                "  ", " ",
            ).like(like_pattern),
            not_already_set,
        )
    ).scalars().all()

    for txn in fuzzy_matches:
        if txn.id not in already_updated:
            txn.category_id = category_id
            already_updated.add(txn.id)

    if already_updated:
        db.flush()

    return len(already_updated)


def _extract_pattern(description: str) -> str:
    """Extract the most meaningful part of a description for rule matching.

    Strips dates, amounts, reference numbers (including alphanumeric codes
    like IPB2603183152823), and common noise tokens so the stable payee
    name / description core remains.
    """
    desc = _normalize(description)
    # Dates
    desc = re.sub(r"\d{2,4}[/-]\d{2}[/-]\d{2,4}", "", desc)
    # Explicit amounts
    desc = re.sub(r"\$[\d,.]+", "", desc)
    # Hash-prefixed references
    desc = re.sub(r"#\d+", "", desc)
    # Pure numeric sequences of 4+ digits (account numbers, amounts)
    desc = re.sub(r"\b\d{4,}\b", "", desc)
    # Alphanumeric tokens that contain 5+ consecutive digits
    # e.g. IPB2603183152823, REF20240301, TXN00123456
    desc = re.sub(r"\b[a-z0-9]*\d{5,}[a-z0-9]*\b", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    if len(desc) > 60:
        desc = desc[:60].rsplit(" ", 1)[0]
    return desc


def _pattern_tokens(pattern: str) -> list[str]:
    """Split a pattern into meaningful words (length ≥ 3)."""
    return [w for w in pattern.lower().split() if len(w) >= 3]


def ask_ollama(
    description: str,
    categories: list[str],
    amount: float | None = None,
) -> str | None:
    """Ask a local Ollama LLM to categorize a transaction.

    Returns category name or None if Ollama is unavailable.
    """
    if amount is not None:
        direction = "credit/income" if amount >= 0 else "debit/expense"
        amount_hint = f" [{direction} {abs(amount):.2f}]"
    else:
        amount_hint = ""

    prompt = (
        "You are a personal finance categorizer. Given a bank transaction "
        "description, pick the single best category from the list below. "
        "Reply with ONLY the category name, nothing else.\n\n"
        f"Categories: {', '.join(categories)}\n\n"
        f"Transaction: {description}{amount_hint}\n\n"
        "Category:"
    )

    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 20},
            },
            timeout=15,
        )
        resp.raise_for_status()
        answer = resp.json().get("response", "").strip()
        clean = answer.strip().strip('"').strip("'").strip(".")

        for cat in categories:
            if cat.lower() == clean.lower():
                return cat
            if cat.lower() in clean.lower():
                return cat

        return None
    except Exception as e:
        log.debug("Ollama unavailable: %s", e)
        return None


def categorize_transaction(
    db: Session,
    transaction: Transaction,
) -> int | None:
    """Try to auto-categorize a single transaction. Returns category_id or None.

    Priority: learned rules > keywords > LLM (Ollama).
    """
    # 1. Learned rules
    rule_cat_id = match_learned_rule(db, transaction.description)
    if rule_cat_id:
        return rule_cat_id

    # 2. Keyword heuristics
    kw_match = match_keyword(transaction.description, amount=transaction.amount)
    if kw_match:
        cat = db.execute(
            select(Category).where(func.lower(Category.name) == kw_match.lower())
        ).scalar_one_or_none()
        if cat:
            return cat.id

    # 3. LLM fallback
    all_cats = db.execute(select(Category.name)).scalars().all()
    if all_cats:
        llm_match = ask_ollama(transaction.description, list(all_cats), amount=transaction.amount)
        if llm_match:
            cat = db.execute(
                select(Category).where(
                    func.lower(Category.name) == llm_match.lower()
                )
            ).scalar_one_or_none()
            if cat:
                return cat.id

    return None


from dataclasses import dataclass


@dataclass
class CategorySuggestion:
    transaction: "Transaction"
    category_id: int | None   # None = no suggestion, user must pick
    category_name: str | None
    method: str   # "rule", "keyword", "llm", or "none"


def suggest_categories(
    db: Session,
    limit: int = 200,
    account_id: int | None = None,
) -> list[CategorySuggestion]:
    """Dry-run categorization — returns ALL uncategorized transactions.

    Transactions where a match is found have category_id/name pre-filled.
    Transactions with no match have category_id=None so the user can pick.
    Used by the preview route so the user can validate before applying.
    """
    q = select(Transaction).where(Transaction.category_id.is_(None))
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    txns = db.execute(
        q.order_by(Transaction.date.desc()).limit(limit)
    ).scalars().all()

    all_cats = db.execute(select(Category)).scalars().all()
    cat_by_id: dict[int, Category] = {c.id: c for c in all_cats}
    cat_list = [c.name for c in all_cats]

    suggestions: list[CategorySuggestion] = []

    for txn in txns:
        # 1. Learned rules
        rule_cat_id = match_learned_rule(db, txn.description)
        if rule_cat_id and rule_cat_id in cat_by_id:
            suggestions.append(CategorySuggestion(
                transaction=txn,
                category_id=rule_cat_id,
                category_name=cat_by_id[rule_cat_id].name,
                method="rule",
            ))
            continue

        # 2. Keywords
        kw_match = match_keyword(txn.description, amount=txn.amount)
        if kw_match:
            cat = db.execute(
                select(Category).where(func.lower(Category.name) == kw_match.lower())
            ).scalar_one_or_none()
            if cat:
                suggestions.append(CategorySuggestion(
                    transaction=txn,
                    category_id=cat.id,
                    category_name=cat.name,
                    method="keyword",
                ))
                continue

        # No match from rules/keywords — include so user can pick, Ollama scores async
        suggestions.append(CategorySuggestion(
            transaction=txn,
            category_id=None,
            category_name=None,
            method="none",
        ))

    return suggestions


def categorize_batch(
    db: Session,
    transaction_ids: list[int] | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """Auto-categorize uncategorized transactions in bulk.

    Returns stats: {rules: N, keywords: N, llm: N, failed: N}.
    """
    query = select(Transaction).where(Transaction.category_id.is_(None))
    if transaction_ids:
        query = query.where(Transaction.id.in_(transaction_ids))
    query = query.limit(limit)

    txns = db.execute(query).scalars().all()
    stats = {"rules": 0, "keywords": 0, "llm": 0, "failed": 0, "total": len(txns)}

    all_cats = db.execute(select(Category.name)).scalars().all()
    cat_list = list(all_cats)

    for txn in txns:
        # 1. Learned rules
        rule_cat_id = match_learned_rule(db, txn.description)
        if rule_cat_id:
            txn.category_id = rule_cat_id
            stats["rules"] += 1
            continue

        # 2. Keywords
        kw_match = match_keyword(txn.description, amount=txn.amount)
        if kw_match:
            cat = db.execute(
                select(Category).where(
                    func.lower(Category.name) == kw_match.lower()
                )
            ).scalar_one_or_none()
            if cat:
                txn.category_id = cat.id
                stats["keywords"] += 1
                continue

        # 3. LLM
        llm_match = ask_ollama(txn.description, cat_list, amount=txn.amount)
        if llm_match:
            cat = db.execute(
                select(Category).where(
                    func.lower(Category.name) == llm_match.lower()
                )
            ).scalar_one_or_none()
            if cat:
                txn.category_id = cat.id
                stats["llm"] += 1
                continue

        stats["failed"] += 1

    db.commit()
    return stats
