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
    "groceries": ["grocery", "whole foods", "trader joe", "kroger", "safeway",
                   "aldi", "costco", "walmart supercenter", "publix", "heb"],
    "dining out": ["restaurant", "mcdonald", "starbucks", "chipotle", "subway",
                   "doordash", "grubhub", "uber eats", "pizza", "cafe", "diner",
                   "burger", "taco", "sushi", "thai", "panda express"],
    "gas / fuel": ["shell", "exxon", "chevron", "bp ", "gas station", "fuel",
                   "mobil", "speedway", "wawa gas", "circle k"],
    "transportation": ["uber", "lyft", "taxi", "transit", "metro", "parking",
                       "toll", "ez pass"],
    "utilities": ["electric", "water bill", "gas bill", "utility", "power",
                  "sewage", "garbage", "waste management", "xcel", "pge",
                  "comcast", "at&t", "verizon", "t-mobile", "internet"],
    "subscriptions": ["netflix", "spotify", "hulu", "disney+", "hbo",
                      "apple music", "youtube premium", "amazon prime",
                      "adobe", "microsoft 365", "icloud"],
    "insurance": ["insurance", "geico", "state farm", "allstate", "progressive",
                  "liberty mutual", "usaa"],
    "healthcare": ["pharmacy", "cvs", "walgreens", "doctor", "hospital",
                   "medical", "dental", "optometrist", "urgent care",
                   "labcorp", "quest diag"],
    "shopping": ["amazon", "target", "best buy", "home depot", "lowes",
                 "ikea", "ebay", "etsy", "nordstrom", "macys"],
    "rent / mortgage": ["rent", "mortgage", "hoa ", "lease"],
    "entertainment": ["cinema", "movie", "theater", "concert", "ticket",
                      "amc ", "regal", "event"],
    "travel": ["airline", "hotel", "airbnb", "booking.com", "expedia",
               "delta", "united", "american air", "southwest", "marriott",
               "hilton"],
    "salary": ["payroll", "direct deposit", "salary", "wage"],
    "interest": ["interest earned", "interest payment", "apy"],
    "account transfer": ["transfer", "xfer", "ach", "wire", "zelle",
                         "venmo", "paypal", "payment thank you",
                         "online payment", "automatic payment",
                         "autopay", "payment received", "payment from"],
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def match_keyword(description: str) -> str | None:
    """Fast keyword-based category guess."""
    desc = _normalize(description)
    for category_name, keywords in KEYWORD_MAP.items():
        for kw in keywords:
            if kw in desc:
                return category_name
    return None


def match_learned_rule(db: Session, description: str) -> int | None:
    """Check stored rules from previous user corrections."""
    desc = _normalize(description)
    rules = db.execute(
        select(CategoryRule).order_by(CategoryRule.hit_count.desc())
    ).scalars().all()

    for rule in rules:
        if rule.pattern.lower() in desc:
            rule.hit_count += 1
            db.commit()
            return rule.category_id
    return None


def learn_from_correction(
    db: Session,
    description: str,
    category_id: int,
) -> tuple[CategoryRule, int]:
    """Store a user's category correction as a learned rule AND retroactively
    update all existing transactions whose description matches the pattern.

    Returns (rule, count_updated).
    """
    pattern = _extract_pattern(description)

    existing = db.execute(
        select(CategoryRule).where(
            CategoryRule.pattern == pattern,
            CategoryRule.category_id == category_id,
        )
    ).scalar_one_or_none()

    if existing:
        existing.hit_count += 1
    else:
        existing = CategoryRule(
            pattern=pattern,
            category_id=category_id,
            source="user_correction",
        )
        db.add(existing)
    db.flush()

    # Retroactively update all matching transactions
    count = _apply_pattern_to_existing(db, pattern, category_id)

    return existing, count


def _apply_pattern_to_existing(
    db: Session,
    pattern: str,
    category_id: int,
) -> int:
    """Update category on all transactions whose description contains the pattern."""
    like_pattern = f"%{pattern}%"
    matching = db.execute(
        select(Transaction).where(
            func.lower(Transaction.description).like(like_pattern),
            Transaction.category_id != category_id,
        )
    ).scalars().all()

    for txn in matching:
        txn.category_id = category_id

    return len(matching)


def _extract_pattern(description: str) -> str:
    """Extract the most meaningful part of a description for rule matching.

    Strips dates, amounts, reference numbers, and common noise words.
    """
    desc = _normalize(description)
    desc = re.sub(r"\d{2,4}[/-]\d{2}[/-]\d{2,4}", "", desc)
    desc = re.sub(r"#\d+", "", desc)
    desc = re.sub(r"\b\d{4,}\b", "", desc)
    desc = re.sub(r"\$[\d,.]+", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    if len(desc) > 50:
        desc = desc[:50].rsplit(" ", 1)[0]
    return desc


def ask_ollama(
    description: str,
    categories: list[str],
) -> str | None:
    """Ask a local Ollama LLM to categorize a transaction.

    Returns category name or None if Ollama is unavailable.
    """
    prompt = (
        "You are a personal finance categorizer. Given a bank transaction "
        "description, pick the single best category from the list below. "
        "Reply with ONLY the category name, nothing else.\n\n"
        f"Categories: {', '.join(categories)}\n\n"
        f"Transaction: {description}\n\n"
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
    kw_match = match_keyword(transaction.description)
    if kw_match:
        cat = db.execute(
            select(Category).where(func.lower(Category.name) == kw_match.lower())
        ).scalar_one_or_none()
        if cat:
            return cat.id

    # 3. LLM fallback
    all_cats = db.execute(select(Category.name)).scalars().all()
    if all_cats:
        llm_match = ask_ollama(transaction.description, list(all_cats))
        if llm_match:
            cat = db.execute(
                select(Category).where(
                    func.lower(Category.name) == llm_match.lower()
                )
            ).scalar_one_or_none()
            if cat:
                return cat.id

    return None


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
        kw_match = match_keyword(txn.description)
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
        llm_match = ask_ollama(txn.description, cat_list)
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
