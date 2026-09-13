"""The model tier of categorisation does as little work as possible.

An import of 279 rows was measured making 209 model calls — fifty of them for
descriptions already asked in the same batch, all of them for merchants the
model had answered before but never taught to the rules table — while
reloading 1,557 rules from the database for every row. Two minutes of a
23 GB model at full tilt, per import, and none of it new information.

Nothing here changes the model or the question it is asked. It changes how
often it is asked.
"""
from decimal import Decimal
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import app.services.categorizer as cat
from app.database import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule
from app.models.transaction import Transaction


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session._engine = engine
    yield session
    session.close()


@pytest.fixture
def world(db):
    acct = Account(name="Card", account_type=AccountType.CREDIT_CARD,
                   currency="USD", is_asset=False)
    groceries = Category(name="Groceries", category_type=CategoryType.EXPENSE)
    transport = Category(name="Transport", category_type=CategoryType.EXPENSE)
    db.add_all([acct, groceries, transport])
    db.flush()
    return {"acct": acct, "groceries": groceries, "transport": transport}


def _txn(db, acct, description, amount="-4.50"):
    t = Transaction(account_id=acct.id, date=datetime(2026, 9, 1),
                    description=description, amount=Decimal(amount),
                    original_currency="USD")
    db.add(t)
    db.flush()
    return t


def _no_keywords(monkeypatch):
    """Force everything past tier two so the model tier is what is tested."""
    monkeypatch.setattr(cat, "match_keyword", lambda d, amount=None: None)


def _stub_batch(monkeypatch, answer_for):
    """Replace the model with a lookup, recording what it was asked."""
    asked = []

    def fake_batch(items, categories):
        asked.append([d for d, _ in items])
        return [answer_for.get(d) for d, _ in items]

    monkeypatch.setattr(cat, "ask_ollama_batch", fake_batch)
    monkeypatch.setattr(cat, "ask_ollama", lambda d, c, amount=None: answer_for.get(d))
    return asked


# ── Memoisation ──────────────────────────────────────────────────────────


def test_identical_descriptions_are_asked_once(db, world, monkeypatch):
    _no_keywords(monkeypatch)
    asked = _stub_batch(monkeypatch, {"TFL TRAVEL CH": "Transport"})
    for _ in range(5):
        _txn(db, world["acct"], "TFL TRAVEL CH")
    db.commit()

    stats = cat.categorize_batch(db)

    assert stats["llm"] == 5
    assert sum(len(batch) for batch in asked) == 1


def test_descriptions_go_to_the_model_in_one_batch(db, world, monkeypatch):
    _no_keywords(monkeypatch)
    answers = {f"MERCHANT {i}": "Groceries" for i in range(12)}
    asked = _stub_batch(monkeypatch, answers)
    for d in answers:
        _txn(db, world["acct"], d)
    db.commit()

    cat.categorize_batch(db)

    assert len(asked) == 1
    assert len(asked[0]) == 12


# ── Learning ─────────────────────────────────────────────────────────────


def test_a_model_answer_becomes_a_rule(db, world, monkeypatch):
    _no_keywords(monkeypatch)
    _stub_batch(monkeypatch, {"OCADO HATFIELD": "Groceries"})
    _txn(db, world["acct"], "OCADO HATFIELD")
    db.commit()

    cat.categorize_batch(db)

    rule = db.execute(select(CategoryRule)).scalar_one()
    assert rule.pattern == "ocado hatfield"
    assert rule.category_id == world["groceries"].id
    assert rule.source == "llm"


def test_the_next_import_never_asks_the_model_again(db, world, monkeypatch):
    """The whole point: the model teaches tier one once."""
    _no_keywords(monkeypatch)
    asked = _stub_batch(monkeypatch, {"OCADO HATFIELD": "Groceries"})
    _txn(db, world["acct"], "OCADO HATFIELD")
    db.commit()
    cat.categorize_batch(db)
    asked.clear()

    later = _txn(db, world["acct"], "OCADO HATFIELD")
    db.commit()
    stats = cat.categorize_batch(db, transaction_ids=[later.id])

    assert stats["rules"] == 1
    assert stats["llm"] == 0
    assert asked == []
    assert later.category_id == world["groceries"].id


def test_a_users_correction_is_never_overwritten_by_the_model(db, world, monkeypatch):
    """Model guesses are additive. A rule that already exists — a user's own
    correction above all — keeps its category."""
    db.add(CategoryRule(pattern="ocado hatfield", category_id=world["transport"].id,
                        source="user_correction", hit_count=3))
    db.commit()

    result = cat._learn_from_llm(db, "OCADO HATFIELD", world["groceries"].id, None)

    assert result is None
    rule = db.execute(select(CategoryRule)).scalar_one()
    assert rule.category_id == world["transport"].id
    assert rule.source == "user_correction"


# ── Scope and cost ───────────────────────────────────────────────────────


def test_transaction_ids_scope_the_run_to_the_new_rows(db, world, monkeypatch):
    """An import must not re-run the model over the ledger's old failures."""
    _no_keywords(monkeypatch)
    asked = _stub_batch(monkeypatch, {"NEW ROW": "Groceries", "OLD ROW": "Groceries"})
    old = _txn(db, world["acct"], "OLD ROW")
    new = _txn(db, world["acct"], "NEW ROW")
    db.commit()

    cat.categorize_batch(db, transaction_ids=[new.id])

    assert [d for batch in asked for d in batch] == ["NEW ROW"]
    assert old.category_id is None


def test_rules_are_loaded_once_per_batch(db, world, monkeypatch):
    _no_keywords(monkeypatch)
    _stub_batch(monkeypatch, {})
    for i in range(30):
        _txn(db, world["acct"], f"ROW {i}")
    db.add(CategoryRule(pattern="zzz", category_id=world["groceries"].id))
    db.commit()

    rule_loads = []

    @event.listens_for(db._engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if "category_rules" in statement and statement.lstrip().upper().startswith("SELECT"):
            rule_loads.append(statement)

    cat.categorize_batch(db)

    assert len(rule_loads) == 1, f"rules loaded {len(rule_loads)} times for 30 rows"


def test_without_a_model_rows_are_left_uncategorised_not_errored(db, world, monkeypatch):
    _no_keywords(monkeypatch)
    _stub_batch(monkeypatch, {})       # every answer None, as with no daemon
    _txn(db, world["acct"], "UNKNOWN SHOP")
    db.commit()

    stats = cat.categorize_batch(db)

    assert stats["failed"] == 1
    assert stats["llm"] == 0


def test_an_item_the_batch_skips_falls_back_to_a_single_call(db, world, monkeypatch):
    """The batch path may only ever reduce calls, never lose an answer the
    old per-row path would have got."""
    _no_keywords(monkeypatch)
    single = []
    monkeypatch.setattr(cat, "ask_ollama_batch", lambda items, cats: [None] * len(items))
    monkeypatch.setattr(cat, "ask_ollama",
                        lambda d, c, amount=None: single.append(d) or "Transport")
    _txn(db, world["acct"], "UBER TRIP")
    db.commit()

    stats = cat.categorize_batch(db)

    assert single == ["UBER TRIP"]
    assert stats["llm"] == 1


# ── Parsing the batch answer ─────────────────────────────────────────────


def test_batch_answer_parsing_accepts_only_known_categories():
    parsed = cat._parse_batch_answer(
        "1 | Groceries\n2. | transport\n3 | Made Up\n4 | Groceries.\nnoise",
        ["Groceries", "Transport"],
    )

    assert parsed == {0: "Groceries", 1: "Transport", 3: "Groceries"}


def test_batch_answer_parsing_survives_garbage():
    assert cat._parse_batch_answer("", ["Groceries"]) == {}
    assert cat._parse_batch_answer("I cannot help", ["Groceries"]) == {}
    assert cat._parse_batch_answer("x | Groceries", ["Groceries"]) == {}
