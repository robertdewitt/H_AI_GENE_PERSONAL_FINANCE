# Financial Truth Model — v1

## Overview

The truth engine assigns every transaction a narrow **economic role**
(`event_type`) and layers provenance, confidence, and staleness metadata
onto balances, reconciliation groups, and payment decompositions.  This
gives agents and services a structured, auditable foundation instead of
relying on raw sign/amount heuristics.

## Core concepts

### 1. EconomicEventType (bridge model)

`event_type` answers "what economic role does this row play?" — not "how
should it appear on a report."  Reporting nuance stays in `Category` and
`PaymentDecomposition`.

| Value | When applied |
|---|---|
| `unclassified` | Default for newly imported rows before classification |
| `external_income` | Inflows to banking accounts (payroll, dividends, refunds) |
| `lifestyle_expense` | Outflows from banking accounts that are not fees or taxes |
| `internal_transfer` | Movement between the user's own accounts |
| `card_purchase` | Charge on a credit card |
| `card_payment_settlement` | Payment received by a credit card account |
| `liability_payment` | Payment on a loan or mortgage |
| `investment_flow` | Contribution or withdrawal on investment/retirement accounts |
| `asset_flow` | Value change on real-estate, vehicle, or collectible accounts |
| `fee` | Bank fees, interest charges, service charges |
| `tax_payment` | Tax-related outflows |

Each classification carries:
- `classification_provenance`: `imported`, `inferred`, `user_confirmed`, `rule_derived`
- `classification_confidence`: 0.0–1.0

### 2. Reconciliation groups

Replace pairwise `TransferLink` with N-member groups that can model
splits, card settlements, and multi-currency transfers.

Each **member** carries explicit currency allocation:
- `allocated_amount_native` (required) — amount in the transaction's own currency
- `allocated_currency` (required) — ISO currency code
- `allocated_amount_base` (optional) — amount in the group's base currency

**Invariant**: members' `allocated_amount_base` should net to zero within
`tolerance_base`.  When `allocated_amount_base` is NULL, the system
converts via FX as of the group's `as_of_date` and flags staleness in
the validation result.

Fee legs can be excluded from the net check via `fee_treatment = exclude_from_net`.

Legacy `TransferLink` is preserved for backward compatibility.

### 3. Payment decomposition

A single liability-payment transaction can be decomposed into components
(principal, interest, escrow, insurance, tax, fee).  The component
amounts should sum to the transaction amount (within tolerance).

### 4. Balance truth sources

Each account declares a `balance_truth_source` that controls how the
system computes its balance:

| Source | Mechanism |
|---|---|
| `transaction_sum` | `SUM(transactions.amount)` — default |
| `latest_statement` | `account.statement_balance` snapshot |
| `latest_valuation` | Most recent `AssetValuation` row |
| `liability_balance` | Statement or principal balance |
| `manual_mark` | `account.current_value` |
| `hybrid` | Transaction sum, falling back to statement |

The rich result (`AccountBalanceResult`) includes:
- `balance_as_of` — when the balance was valid
- `balance_stale` — whether it exceeds freshness thresholds
- `balance_confidence` — 0.0–1.0
- `balance_source_used` — which dispatch path was taken
- FX metadata (`fx_pair`, `fx_rate_date`, `fx_stale`) when conversion was needed

### 5. Staleness semantics

All truth-bearing objects carry as-of / stale signals:

| Domain | Fields |
|---|---|
| Balances | `balance_as_of`, `balance_stale`, `balance_confidence` |
| Valuations | Valuation row `date`; `valuation_as_of` in API |
| Liability balances | `statement_balance_as_of`, `liability_balance_stale` |
| FX | `fx_rate_date`, `fx_stale` (rate date != requested date) |
| Confidence summaries | `as_of` on `DataQualityReport` |

### 6. Data quality

The `DataQualityReport` returns:

1. **`blockers`** — conditions that make the ledger unusable for close (e.g. >50% uncategorized, no transactions, too many unreconciled transfers)
2. **`warnings`** — conditions that reduce confidence (e.g. stale balances, unclassified transactions)
3. **`close_readiness_score`** — a 0–100 convenience metric derived from blocker/warning counts; **secondary** to the structured lists above

Agents should enumerate blockers and warnings rather than relying on the
score alone.

## Migration

The schema is additive — new columns and tables only.  For SQLite,
`init_db()` runs idempotent `ALTER TABLE ... ADD COLUMN` statements via
`PRAGMA table_info` checks.  Existing rows get sensible defaults
(`event_type = 'unclassified'`, `balance_truth_source = 'transaction_sum'`).

New tables (`reconciliation_groups`, `reconciliation_members`,
`payment_decompositions`) are created by `Base.metadata.create_all`.

## Remaining gaps (post-v1)

- **Automatic reconciliation linking**: the system detects transfer
  candidates but does not auto-create `ReconciliationGroup` entries.
- **FX rate staleness propagation**: conversion warns but does not block.
- **Hybrid balance heuristics**: the threshold for "prefer statement over
  txn sum" is fixed, not user-configurable.
- **Decomposition auto-inference**: mortgage/loan payment breakdowns must
  be entered manually or via import; no rule engine yet.
- **Multi-currency reconciliation**: FX conversion in invariant checks
  depends on stored rates; missing rates produce warnings, not blockers.
- **Retroactive reclassification**: changing `event_type` on historical
  rows does not cascade to reconciliation groups or decomposition rows.
