"""A PDF statement whose parsed rows don't add up to its own balance change
is read by the local model instead — and only if the model's rows do."""
from datetime import date

import pandas as pd
import pytest

from app.config import settings
from app.services import pdf_import as pi

CITI_TEXT = """Billing Period: 10/16/26-11/16/26
Member Since 2017.
New balance as of 11/16/26: $99.00
Fees Charged
Date Description Amount
11/16 MEMBERSHIP FEE NOV 26-OCT 27
SEE REVERSE FOR MORE RENEWAL INFORMATION $99.00
"""


@pytest.fixture
def statement(monkeypatch, tmp_path):
    f = tmp_path / "citi.pdf"
    f.write_bytes(b"%PDF-stub")
    monkeypatch.setattr(settings, "pdf_llm_fallback", True)
    monkeypatch.setattr(pi, "extract_cc_metadata",
                        lambda p: {"previous_balance": 0.0, "new_balance": 99.0})

    class _Page:
        def extract_text(self):
            return CITI_TEXT

    class _Pdf:
        pages = [_Page()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda p: _Pdf())
    pi._LLM_CACHE.clear()
    return str(f)


def _model_says(monkeypatch, payload: str):
    calls = []
    import app.services.ollama_client as oc
    monkeypatch.setattr(oc, "generate", lambda prompt, **kw: calls.append(prompt) or payload)
    return calls


def test_billing_period_sets_the_year_of_a_bare_date():
    assert pi._statement_period(CITI_TEXT) == (date(2026, 10, 16), date(2026, 11, 16))
    df = pi._parse_statement_text(CITI_TEXT)
    assert list(df["Date"]) == ["2026-11-16"]           # not 2017, from "Member Since"


def test_unreconciled_rows_are_replaced_by_the_models(monkeypatch, statement):
    calls = _model_says(monkeypatch,
        '{"transactions": [{"date": "2026-11-16", "description": "MEMBERSHIP FEE", "amount": 99.0}]}')
    misread = pd.DataFrame([{"Date": "2026-11-16", "Description": "MEMBERSHIP FEE", "Amount": "27"}])

    out = pi._llm_fallback(statement, misread)

    assert out.to_dict("records") == [{"Date": "2026-11-16", "Description": "MEMBERSHIP FEE", "Amount": "99.0"}]
    assert "must add up to 99.00" in calls[0]


def test_a_model_answer_that_does_not_reconcile_is_ignored(monkeypatch, statement):
    _model_says(monkeypatch,
        '{"transactions": [{"date": "2026-11-16", "description": "FEE", "amount": 98.0}]}')
    misread = pd.DataFrame([{"Date": "2026-11-16", "Description": "FEE", "Amount": "27"}])

    assert pi._llm_fallback(statement, misread) is None


def test_rows_that_already_reconcile_never_reach_the_model(monkeypatch, statement):
    calls = _model_says(monkeypatch, "{}")
    good = pd.DataFrame([{"Date": "2026-11-16", "Description": "FEE", "Amount": "99.00"}])

    assert pi._llm_fallback(statement, good) is None
    assert calls == []


def test_switched_off_it_does_nothing(monkeypatch, statement):
    monkeypatch.setattr(settings, "pdf_llm_fallback", False)
    calls = _model_says(monkeypatch, "{}")

    assert pi._llm_fallback(statement, pd.DataFrame()) is None
    assert calls == []
