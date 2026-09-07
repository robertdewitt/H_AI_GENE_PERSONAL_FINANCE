"""Naming columns the hint lists don't recognise.

detect_columns matches header text against fixed hints — "date", "memo",
"payee" — which covers the common exports and misses everything else. A German
statement maps to nothing at all.

The model names columns; the importer still reads the values. Numbers never
pass through it, so the worst case is a wrong column, which is visible on the
mapping screen the user confirms before anything is imported.
"""
import pandas as pd
import pytest

from app.services import column_mapping_llm as cm
from app.services.import_service import detect_columns

GERMAN_COLUMNS = ["Buchungstag", "Buchungstext", "Betrag", "Waehrung", "Saldo"]
GERMAN_ROWS = [{
    "Buchungstag": "01.02.2026", "Buchungstext": "REWE MARKT GMBH",
    "Betrag": "-42,17", "Waehrung": "EUR", "Saldo": "1234,56",
}]


def _model(monkeypatch, answer):
    monkeypatch.setattr(cm, "generate", lambda *a, **k: answer, raising=False)
    import app.services.ollama_client as client
    monkeypatch.setattr(client, "generate", lambda *a, **k: answer)


# ── The gap being filled ─────────────────────────────────────────────────


def test_the_heuristics_really_do_miss_this_shape():
    """Establishes the premise rather than assuming it."""
    mapping = detect_columns(pd.DataFrame(GERMAN_ROWS))

    assert not mapping.get("date")
    assert not mapping.get("description")
    assert not mapping.get("amount")


def test_the_model_names_the_columns(monkeypatch):
    _model(monkeypatch, (
        "date | Buchungstag\n"
        "description | Buchungstext\n"
        "amount | Betrag\n"
        "balance | Saldo\n"
        "currency | Waehrung\n"
        "debit | none\n"
        "credit | none"
    ))

    got = cm.detect_columns_llm(GERMAN_COLUMNS, GERMAN_ROWS)

    assert got == {
        "date": "Buchungstag", "description": "Buchungstext",
        "amount": "Betrag", "balance": "Saldo", "currency": "Waehrung",
    }


# ── Guards ───────────────────────────────────────────────────────────────


def test_a_column_that_does_not_exist_is_discarded(monkeypatch):
    """The model must not be able to invent a column name."""
    _model(monkeypatch, (
        "date | Buchungstag\n"
        "description | Imaginary Column\n"
        "amount | Betrag"
    ))

    got = cm.detect_columns_llm(GERMAN_COLUMNS, GERMAN_ROWS)

    assert got == {"date": "Buchungstag", "amount": "Betrag"}


def test_unknown_field_names_are_ignored(monkeypatch):
    _model(monkeypatch, "date | Buchungstag\nvibe | Betrag")

    assert cm.detect_columns_llm(GERMAN_COLUMNS, GERMAN_ROWS) == {
        "date": "Buchungstag",
    }


def test_none_answers_leave_the_field_unset(monkeypatch):
    _model(monkeypatch, "date | Buchungstag\nbalance | none\ncredit | -")

    got = cm.detect_columns_llm(GERMAN_COLUMNS, GERMAN_ROWS)

    assert "balance" not in got and "credit" not in got


def test_no_model_means_no_mapping(monkeypatch):
    _model(monkeypatch, None)

    assert cm.detect_columns_llm(GERMAN_COLUMNS, GERMAN_ROWS) == {}


def test_no_columns_does_not_call_the_model(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not have been called")

    import app.services.ollama_client as client
    monkeypatch.setattr(client, "generate", _boom)

    assert cm.detect_columns_llm([], []) == {}


# ── Only filling gaps ────────────────────────────────────────────────────


def test_it_never_overrides_what_the_heuristics_decided(monkeypatch):
    _model(monkeypatch, "date | Betrag\ndescription | Buchungstext\namount | Betrag")
    heuristic = {"date": "Buchungstag", "description": None, "amount": None}

    merged, filled = cm.fill_mapping_gaps(heuristic, GERMAN_COLUMNS, GERMAN_ROWS)

    assert merged["date"] == "Buchungstag"      # kept, not replaced
    assert merged["description"] == "Buchungstext"
    assert "date" not in filled


def test_a_complete_mapping_skips_the_model_entirely(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not have been called")

    import app.services.ollama_client as client
    monkeypatch.setattr(client, "generate", _boom)
    complete = {"date": "Buchungstag", "description": "Buchungstext",
                "amount": "Betrag"}

    merged, filled = cm.fill_mapping_gaps(complete, GERMAN_COLUMNS, GERMAN_ROWS)

    assert merged == complete
    assert filled == []


def test_debit_credit_pair_counts_as_having_an_amount(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not have been called")

    import app.services.ollama_client as client
    monkeypatch.setattr(client, "generate", _boom)
    mapping = {"date": "D", "description": "T", "amount": None,
               "debit": "Out", "credit": "In"}

    merged, filled = cm.fill_mapping_gaps(mapping, ["D", "T", "Out", "In"], [])

    assert filled == []


def test_preview_reports_which_fields_the_model_supplied(monkeypatch, tmp_path):
    """The mapping screen needs to show which choices were not made by the
    usual rules."""
    _model(monkeypatch, (
        "date | Buchungstag\ndescription | Buchungstext\namount | Betrag"
    ))
    path = tmp_path / "de.csv"
    path.write_text(
        "Buchungstag,Buchungstext,Betrag\n01.02.2026,REWE MARKT,-42.17\n"
    )
    from app.services.import_service import preview_file

    preview = preview_file(str(path))

    assert set(preview["llm_filled"]) >= {"date", "description", "amount"}
    assert preview["mapping"]["description"] == "Buchungstext"
