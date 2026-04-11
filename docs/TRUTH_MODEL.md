# Financial Truth Model — v2

## Architecture

```
Raw Import → Canonical Model → Truth Engine → Time-Series/MTM → Reporting → Agent Context
```

This is NOT a budgeting app. It is a multi-currency, multi-account,
multi-asset financial truth infrastructure layer. Raw imported rows are
NOT truth. Categories are NOT economic semantics. Transfers must be
reconciled, not tagged.

---

## Core Principles

1. Raw imported rows are NOT truth
2. Categories are NOT economic semantics
3. Transfers must be reconciled, not tagged
4. Credit card payments are NOT new spend
5. Mortgage principal is NOT spend
6. Every important value has: provenance, confidence, as_of_date
7. Agents must never operate on unqualified data

---

## 1. TransactionSplit (Canonical Allocation)

A single transaction supports multiple semantic allocations. Splits are
the authoritative source for spend analysis — raw transaction amounts
are never used directly.

| Field | Type | Purpose |
|---|---|---|
| `amount_native` | float | Amount in transaction currency |
| `currency` | str | ISO currency code |
| `amount_base` | float? | Converted to base currency |
| `fx_rate` | float? | Rate used for conversion |
| `event_type` | str? | Economic role of this allocation |
| `category_id` | int? | Reporting category |
| `linked_account_id` | int? | Counterparty account |
| `linked_reconciliation_group_id` | int? | Reconciliation link |
| `counts_as_true_spend` | bool | Whether this is real economic spend |
| `spend_type` | SpendType? | lifestyle, fixed_core, debt_cost, tax, non_spend_cash_use |
| `provenance` | str? | How this split was derived |
| `confidence` | float? | 0.0–1.0 |

**Invariant**: Sum of splits == parent transaction amount (within tolerance)

### SpendType enum

| Value | Meaning |
|---|---|
| `lifestyle` | Discretionary spending |
| `fixed_core` | Non-discretionary fixed costs |
| `debt_cost` | Interest, fees on debt |
| `tax` | Tax payments |
| `non_spend_cash_use` | Transfers, savings, investments |

---

## 2. EconomicEventType (Bridge Layer)

Maps each transaction to its narrow economic role. NOT a reporting
taxonomy — that stays in Category.

| Value | When applied |
|---|---|
| `unclassified` | Default before classification |
| `external_income` | Generic inflows |
| `payroll_income` | Payroll-detected inflows |
| `employer_benefit` | Non-cash employer contributions |
| `lifestyle_expense` | Outflows that are real spend |
| `internal_transfer` | Between user's own accounts |
| `card_purchase` | Credit card charges |
| `card_payment_settlement` | Payments received by credit card |
| `liability_payment` | Loan payments |
| `mortgage_payment` | Mortgage payments (composite) |
| `mortgage_interest` | Interest component of mortgage |
| `mortgage_principal` | Principal component of mortgage |
| `investment_contribution` | Inflows to investment accounts |
| `investment_withdrawal` | Outflows from investment accounts |
| `investment_flow` | Legacy catch-all for investment activity |
| `asset_flow` | Real estate, vehicle, collectible changes |
| `fee` | Bank fees, service charges |
| `tax_payment` | Tax-related outflows |

Each classification carries:
- `classification_provenance`: imported, inferred, user_confirmed, rule_derived
- `classification_confidence`: 0.0–1.0

---

## 3. Reconciliation Engine

N-member reconciliation groups replace pairwise transfer links.

### ReconciliationGroup

| Field | Purpose |
|---|---|
| `group_type` | transfer, card_settlement, loan_payment, split |
| `status` | suggested, matched, partial, confirmed, rejected |
| `base_currency` | Currency for net-zero check |
| `tolerance_base` | Allowed residual |
| `fee_treatment` | exclude_from_net, include_in_net, separate_line |
| `fx_treatment` | none, spot_on_group_date, member_rates, explicit |
| `fx_rate_used` | Explicit FX rate for the group |
| `reconciliation_confidence` | Confidence in the match |
| `confidence` | Overall group confidence |

### ReconciliationMember

| Field | Purpose |
|---|---|
| `allocated_amount_native` | Amount in transaction's currency |
| `allocated_currency` | ISO currency code |
| `allocated_amount_base` | Amount in group base currency |
| `role` | source, destination, fee |
| `is_fee_leg` | Excluded from net when fee_treatment = exclude |

**Invariant**: Members' allocated_amount_base nets to zero within tolerance.

---

## 4. Payment Decomposition

Breaks liability payments into components:

| Component | Purpose |
|---|---|
| `principal` | Principal paydown (NOT spend) |
| `interest` | Interest charge (debt cost) |
| `escrow` | Escrow payment |
| `insurance` | Insurance premium |
| `tax` | Property tax component |
| `fee` | Fees |

**Invariant**: Component sum == transaction amount

---

## 5. Payroll Decomposition (via Splits)

| Component | Economic Role |
|---|---|
| `salary_gross` | Full pre-deduction amount |
| `payroll_tax` | Tax payment |
| `pension_contribution_employee` | Investment contribution |
| `pension_contribution_employer` | Employer benefit (non-cash) |
| `health_benefit` | Employer benefit |
| `wellbeing_benefit` | Employer benefit |
| `other_deduction` | Fixed cost |
| `net_salary_cash` | Cash deposited |

Non-cash events (employer pension match) are representable even without
a corresponding cash flow on the deposit transaction.

---

## 6. Balance Truth Sources

Each account declares how its balance is computed:

| Source | Mechanism |
|---|---|
| `transaction_sum` | SUM(transactions.amount) — default |
| `latest_statement` | account.statement_balance snapshot |
| `latest_valuation` | Most recent AssetValuation row |
| `liability_balance` | Statement or principal balance |
| `manual_mark` | account.current_value |
| `hybrid` | Transaction sum, falling back to statement |

Every balance result includes:
- `balance_as_of` — temporal anchor
- `balance_stale` — exceeds freshness threshold
- `balance_confidence` — 0.0–1.0
- `balance_source_used` — which dispatch path
- FX metadata when currency conversion needed

---

## 7. MTM Time Series (Snapshot Models)

Point-in-time snapshots store best-known values even if stale:

| Model | Purpose |
|---|---|
| `AccountBalanceSnapshot` | Per-account balance at a date |
| `AssetValuationSnapshot` | Asset values over time |
| `LiabilityBalanceSnapshot` | Liability balances over time |
| `HouseholdSnapshot` | Aggregate balance sheet at a date |

All carry: `as_of_date`, `value_native`, `value_base`, `currency`,
`fx_rate`, `source`, `confidence`, `stale_flag`

HouseholdSnapshot additionally tracks: `accounts_included`,
`stale_accounts`, `low_confidence_accounts`

---

## 8. Attribution Engine

Explains net worth change:

```
ΔNW = contributions + market_movement + fx_movement
    + principal_paydown + fees_and_interest + revaluation
    + spending + tax_payments + unexplained
```

Each component carries `amount_base`, `confidence`, and `notes`.
Market movement and FX movement require valuation time series
(placeholder until full implementation).

---

## 9. Data Quality Engine

### DataQualityReport

| Priority | Contents |
|---|---|
| **Primary** | `blockers` — conditions that make ledger unusable |
| **Primary** | `warnings` — conditions that reduce confidence |
| **Secondary** | `close_readiness_score` — derived 0–100 metric |

### Structured Counters

| Counter | What it measures |
|---|---|
| `uncategorized_count` | Transactions without category |
| `unclassified_count` | Transactions without event_type |
| `low_confidence_count` | Classifications below 0.5 confidence |
| `unresolved_reconciliation_count` | Transfers without reconciliation |
| `stale_valuation_count` | Accounts with stale balances |
| `liabilities_without_decomposition` | Liability payments without component breakdown |
| `missing_fx_count` | Accounts with stale FX rates |
| `unsplit_transaction_count` | Transactions without split allocations |

Blockers > score. Agents should enumerate blockers and warnings.

---

## 10. Agent Context Layer

Agents receive full structured payloads with:

- Balance sheet with per-account confidence and freshness
- True spend breakdown from splits (not raw rows)
- Time series snapshots
- Data quality report with counters
- Object-level confidence and staleness

**Agents must never operate on unqualified data.**

---

## 11. API Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/balance-sheet` | Full household balance sheet with confidence |
| `GET /api/v1/spending/true-spend` | Spend from splits only |
| `GET /api/v1/data-quality` | Blockers, warnings, counters, score |
| `GET /api/v1/agent/context` | Comprehensive agent payload |
| `GET /api/v1/accounts` | Account listing with balances |
| `GET /api/v1/transactions` | Filtered transaction listing |
| `GET /api/v1/net-worth` | Current net worth snapshot |
| `GET /api/v1/net-worth/history` | Monthly NW history |
| `GET /api/v1/spending/by-category` | Category spending breakdown |
| `GET /api/v1/spending/monthly` | Monthly income vs spending |
| `GET /api/v1/spending/top-merchants` | Top merchants by spend |
| `GET /api/v1/documents/payroll` | Payroll payslip document time series |
| `GET /api/v1/rental-properties` | Rental property entities |
| `GET /api/v1/rental-properties/{id}/pnl` | Property P&L snapshot time series |

---

## 12. Structured multi-line documents

Payslips and rental statements are **first-class documents**, not inferred
from a single bank row.

### DocumentLineKind

| Kind | Meaning |
|---|---|
| `income` | Inflows (gross pay, rent, employer match when non-cash tracked separately) |
| `expense` | Withholdings, operating costs, taxes |
| `transfer` | Owner draw / distribution (not operating expense) |
| `liability` | Balance-sheet adjustments (e.g. deferred / prepaid rent) |

### JSON format

- **Payroll**: requires `net_pay` equal to the sum of all lines that are
  **not** marked `excluded_from_net_sum` (e.g. employer 401k match is
  excluded — stored as a line with split amount `0` for non-cash truth).
- **Rental**: requires `net_bank_deposit` equal to the same rule; prepaid
  rent lines may be `excluded_from_net_sum` so they affect P&L liability
  but not the bank deposit split sum.

### Persistence

- `financial_documents` + `financial_document_lines` store every line with
  provenance and raw JSON.
- Parent `transactions` reference `financial_document_id`; each
  `transaction_split` can reference `document_line_id`.
- **Property P&L time series**: `property_pnl_snapshots` rolls up income,
  expense, owner draw, liability adjustment, NOI, and net cash flow per
  statement period.

### Services

- `document_parse.parse_document_dict` / `parse_document_json` — validate
  and build `ParsedFinancialDocument`.
- `document_apply.apply_financial_document` — persist document, lines,
  transaction, splits, and rental P&L snapshot.

Sample fixtures: `tests/fixtures/documents/payroll_payslip_sample.json`,
`tests/fixtures/documents/rental_statement_sample.json`.

---

## Migration

All changes are additive:

- New tables: `transaction_splits`, `account_balance_snapshots`,
  `asset_valuation_snapshots`, `liability_balance_snapshots`,
  `household_snapshots`, `rental_properties`, `financial_documents`,
  `financial_document_lines`, `property_pnl_snapshots`
- New columns: `reconciliation_groups.reconciliation_confidence`,
  `reconciliation_groups.fx_rate_used`, `transactions.financial_document_id`,
  `transaction_splits.document_line_id`
- `init_db()` runs idempotent `ALTER TABLE ADD COLUMN` for SQLite
- `Base.metadata.create_all` handles new table creation
- Existing data gets sensible defaults

---

## Remaining Gaps (post-implementation)

- **Market movement**: implemented via `AssetValuation` start/end diffs per
  asset account; thin or stale valuation history still reduces confidence.
- **FX movement**: implemented as translation of period-end native
  balances at start vs end FX (approximation; not a full cash-flow FX
  attribution).
- **Auto reconciliation**: `POST /api/v1/reconciliation/auto-suggest`
  creates **suggested** `ReconciliationGroup` rows for transfer pairs;
  does not replace human review for ambiguous cases.
- **Split auto-generation**: imports get a **pass-through** split via
  `ensure_splits_after_import`; complex rules still require edit UI or
  structured documents.
- **Positions / prices**: `Instrument`, `PositionLot`, and `PriceSnapshot`
  models exist; brokerage sync, UI, and lot-level reconciliation are not
  built out.
- **Retroactive reclassification**: editing `event_type` cascades to
  **manual** splits (no `document_line_id`); payment decompositions are
  **flagged** with a stale note rather than auto-resized.
- **Multi-currency reconciliation**: missing FX for multi-currency
  **reconciliation groups** is now a **data-quality blocker** (not only
  a warning).
