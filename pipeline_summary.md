# RILA Validation Program— Architecture Overview

## Pipeline Flow

```
Main_run_validation_process.py
  ├── validation_process.py       → Data fetch, transform, rollforward → pols_structure.json
  ├── post_process.py             → pols_structure.json → summaries, difference reports
  ├── compare_program.py          → pols_structure.json + system .txt → comparison Excel files
  ├── logger.py                   → logging infrastructure
  └── demo_email.py               → email notification (currently disabled)
```

---

## 1. `Main_run_validation_process.py` — Orchestrator

**Purpose:** Entry point that sequences the entire validation pipeline.

**Configuration:**


| Config Key       | Value                                                                                                       |
| ---------------- | ----------------------------------------------------------------------------------------------------------- |
| `UI_file_name`   | `RILA_Validation_UI_v1_Step1.xlsx` — User inputs: cycle date, policy filter, SQL file names, fund mappings |
| `surr_file_path` | `RILA_SC.csv` — Surrender values                                                                           |
| `input_base`     | `C:\AAA_RILA_Validation_Modernization\RILA_Python\Input_RILA`                                               |
| `output_base`    | `C:\AAA_RILA_Validation_Modernization\RILA_Python\Output_RILA`                                              |

**Execution Steps:**

1. **Setup** — Creates a unique `run_id` (UUID), creates output directory, initializes logging
2. **Validation** — Calls `run_validation_process()` → fetches data, runs rollforwards, produces `pols_structure.json`
3. **Post-processing** — Calls `run_post_process()` → reads JSON, generates summaries and difference lists with $10 threshold checks
4. **Comparison** — Calls `run_compare_process()` → compares program-calculated values against system actuarial text file, produces comparison Excel files
5. **Email** — Optionally sends notification email (currently `if False`, disabled)
6. **Timing** — Logs total elapsed time

---

## 2. `validation_process.py` — Core Validation Engine

**Purpose:** The heavy lifter. Fetches raw data, transforms it, and runs actuarial rollforward calculations.

### Key Functions


| Function                                                     | Purpose                                                                                                                              |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `run_validation_process()`                                   | Main orchestrator. Reads UI Excel, determines cycle date, fetches data, unpivots, runs rollforwards                                  |
| `read_excel_user_inputs()`                                   | Extracts cycle date, policy filter list, and SQL file names from the UI Excel                                                        |
| `read_excel_fund_mapping()`                                  | Reads fund code mappings → produces`FUND_TXN_MAP`, `FUND_INDEX_MAP`, `FUND_EQINDEX_MAP`, `FUND_CODE_MAP`                            |
| `retrieve_data_from_Navisys()`                               | Connects to Navisys database, runs SQL queries, writes raw CSVs to a date-stamped folder                                             |
| `build_data_long_from_sources()` → `unpivot_gmdb_by_policy` | Transforms wide-format transaction data (one column per fund) into long format:`(policy, raw_fund_code, PRNL_amt, TXN_AMT, SYS_MWV)` |
| `RollForward_base.get_rollforward_from_extract()`            | Fund-level rollforward calculation → produces`rollforward_results` DataFrame and `pols_dict` nested structure                       |
| `RollForward_policy.get_rollforward_from_extract()`          | Policy-level rollforward calculation → enriches`pols_dict`                                                                          |

### 2a. `RollForward_base.get_rollforward_from_extract()` — Fund-Level Rollforward Engine

**Granularity:** `(POLICY_NUM, raw_fund_code)` — operates at the fund level within each policy.

**Inputs:**


| Parameter          | Type           | Description                                                                                                                                                                                                                                                         |
| ------------------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data_by_fund`     | `pd.DataFrame` | Transaction-level data grouped by fund:`POLICY_NUM`, `raw_fund_code`, `EFFECTIVE_DATE`, `TRANSACTION_NUM`, `TRANSACTION_DESC`, `TXN_AMT`, `ACCOUNT_VAL`, `GrWd+PErecap`, `PRNL_amt`, `MWV Rate%`, `fixed rate`, `freelook rate`, and various index/OUV rate columns |
| `results_basepath` | `str`          | Output directory for CSVs and JSON artifacts                                                                                                                                                                                                                        |
| `file_list`        | `dict`         | Maps keys (`"Daily equity index quotes"`, `"Daily OUV values"`, `"Daily VIX quotes"`, `"Auto Performance Capture Funds"`) to file paths                                                                                                                             |
| `surr_file_path`   | `str`          | Path to CSV surrender charge table                                                                                                                                                                                                                                  |
| `data_by_txn`      | `pd.DataFrame` | Policy-level transaction data (not fund-granular), used for merging aggregated AV back                                                                                                                                                                              |
| `fund_mapping`     | `dict`         | Contains`FUND_TXN_MAP`, `FUND_INDEX_MAP`, `FUND_EQINDEX_MAP`, `FUND_CODE_MAP` — maps fund codes to strategy parameters and index names                                                                                                                             |

**Processing Steps:**

1. **Unpack fund mappings** — stores `FUND_TXN_MAP`, `FUND_INDEX_MAP`, `FUND_EQINDEX_MAP`, `FUND_CODE_MAP` for lookup during calculations.
2. **Load and cache reference data:**

   - **Equity index time series** (`_prep_index_df()`) — SP500_INDEX, NAS_DQ, etc. indexed by date
   - **OUV table** (`_prep_ouv_df()`) — Option Upfront Value keyed by `(PO_TERM_START_DATE, PO_FUND_CODE, PO_VALUATION_DATE)`, containing buffer, cap, participation rate, trigger rate, and lock rate parameters
   - **VIX table** (`_prep_vix_df()`) — volatility index values by date (used for trading cost calculation)
   - **Auto Performance Capture funds** (`_prep_alfunds_df()`) — fund configuration for auto-lock detection
   - **Surrender charge dict** (`load_surr_chrg_table_to_dict()`) — nested dict: `ADMIN_SYSTEM → STATE_CD → PROD_DESC → PROD_DESC_CD_index → YR_NUM_index → charge rate`
3. **Sort** `data_by_fund` by `["POLICY_NUM", "raw_fund_code"]` (stable mergesort) to ensure chronological processing per fund.
4. **Main processing loop** — iterates each row, maintaining a `states: Dict[Tuple[str, str], GMDBState]` keyed by `(policy_num, fund_code)`:

   - **Boundary detection** — compares current row to previous row on `POLICY_NUM`, `EFFECTIVE_DATE`, `TRANSACTION_NUM` to set `newPol`, `newDay`, `newTransaction` flags
   - **Aggregate previous transaction** — when `newTransaction=True`, calls `cls.aggregate()` to sum `av_end_txn` across all fund states for the previous `(policy, txn)`. This aggregate (`last_aggregate`) is carried forward to every fund row within the same transaction.
   - **Per-fund rollforward** (`cls.process_row()`) executes the full calculation pipeline:


   | Sub-step                      | Purpose                                                                                                                                                                                  |
   | ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `prepopulate_state()`         | Extracts transaction properties into the state object                                                                                                                                    |
   | `get_trxn_amount()`           | Classifies as premium, withdrawal, or fee; tracks cumulative withdrawals (`accum_wd`), resets on `Index Valuation` or reset-type txns                                                    |
   | `calculate_timing()`          | Sets boundary flags, deposit count, anniversary flags, termination flag                                                                                                                  |
   | `calculate_t()`               | Computes`t` (time from BOP), `dt` (delta time from prior EOP), `pol_yr`, `remaining_time_pct`, and dates (`bop_date`, `eop_date`, `eot_date`)                                            |
   | `calculate_crediting_time()`  | Determines if index crediting occurs now (fixed funds on new days, or index funds at term-end Index Valuation)                                                                           |
   | `get_fund_parameter()`        | Extracts cap, buffer, participation rate, trigger rate, lock rate from row columns based on`FUND_INDEX_MAP`                                                                              |
   | `get_index_return()`          | Looks up current index value; computes`(current / last) - 1` at crediting points                                                                                                         |
   | `get_max_index_return()`      | For LOCK_FUNDS (`RO`, `RN`, `RM`, `RP`, `RR`, `RQ`), tracks max index level during the term for lock threshold detection                                                                 |
   | `lookup_ouv_value()`          | Looks up BOP and EOP OUV from cached table, keyed by index rate parameters, term start date, strategy code, and txn date                                                                 |
   | `lookup_vix_tc_value()`       | Computes trading cost as % of EOP OUV using VIX level (4%/1% normally; 12%/3% for exception funds`RO`, `RN`, `RM`)                                                                       |
   | `lookup_max_ouv_value()`      | For auto performance capture funds, tracks max OUV gain to determine capture threshold                                                                                                   |
   | `get_strat_base()`            | Calculates strategy base, adjusting for withdrawals (pro-rata by fund portion), fees, and OUV-adjusted interim values                                                                    |
   | `calculate_strategy_return()` | Applies fund-specific return logic based on fund code (see table below)                                                                                                                  |
   | `calculate_av()`              | Rolls forward Account Value: applies strategy return to`strat_base` for crediting events, applies OUV adjustment for interim valuations, handles withdrawals/fees as pro-rata deductions |
   | `get_surrender_chrg()`        | Looks up surrender charge rate; calculates charge on amounts exceeding the 10% free withdrawal amount (`fwv`), prorated by fund portion                                                  |
   | `calculate_mwv()`             | Calculates Minimum Withdrawal Value (guaranteed floor), grows at MWV rate for fixed funds, resets at reallocations, adjusts for withdrawals, caps at 87.5% of AV for locked funds        |

   **Fund-specific strategy return logic** (`calculate_strategy_return()`):


   | Fund Codes                         | Strategy Type                | Return Logic                                                  |
   | ---------------------------------- | ---------------------------- | ------------------------------------------------------------- |
   | `RZ`, `TB`                         | Fixed funds                  | Uses fixed rate                                               |
   | `H6`                               | Freelook                     | Uses freelook rate                                            |
   | `RW`, `RV`                         | Buffer + Participation       | Downside protected by buffer, full upside participation       |
   | `RB`, `RC`, `RD`, `RG`, `RH`       | Buffer + Cap                 | Downside buffered, upside capped                              |
   | `RJ`, `RI`, `RK`, `RL`             | Dual Direction w/Cap         | Gains on both positive and negative index movement, capped    |
   | `TC`, `TD`                         | Buffer + Participation + Cap | Participation rate on upside, buffer on downside, cap         |
   | `RO`, `RN`, `RM`, `RP`, `RR`, `RQ` | Lock funds                   | Locked rate if threshold achieved, otherwise buffer-protected |
   | `RE`, `RF`                         | Buffer + Trigger             | Trigger rate on positive, buffer on negative                  |
   | `TL`                               | Dual Direction Trigger Cap   | Fixed trigger rate                                            |
5. **Combine aggregate + per-fund results** — each row gets `total_fund_value` from the aggregate step plus all per-fund computed columns.
6. **Save intermediate output** — writes `2. Data_Txn_Level_wRollforward.csv`.
7. **Resort and build nested dict** — re-sorts by `["POLICY_NUM", "EFFECTIVE_DATE", "TT_TXN_TYPE_CODE", "raw_fund_code"]`, then calls `PolicyTrxnDataJson.build_policies_structure()` to build hierarchical `OrderedDict`: `policy → day → transaction → fund`.
8. **Aggregate AV back to policy level** — groups `av_end_txn` and `mwv_interim` by `(POLICY_NUM, TRANSACTION_NUM)`, merges sums into `data_by_txn`, writes `3. Data_Pol_Level_wAV.csv`.

**Outputs:**


| Output                               | Type           | Description                                                                                                  |
| ------------------------------------ | -------------- | ------------------------------------------------------------------------------------------------------------ |
| `rollforward_results`                | `pd.DataFrame` | All input columns plus ~70 computed columns (see below)                                                      |
| `pols_dict`                          | `dict`         | Nested`OrderedDict`: `policy_id → {num, days → {date, transactions → {transaction_num, funds → {...}}}}` |
| `2. Data_Txn_Level_wRollforward.csv` | File           | Intermediate rollforward results at txn+fund granularity                                                     |
| `3. Data_Pol_Level_wAV.csv`          | File           | Policy-level data with aggregated`av_end_txn` and `mwv_interim`                                              |

**Key computed columns** (appended to input DataFrame):

- Transaction classification: `Prem`, `Fee`, `Wd`, `accum_wd`, `is_withdrawal`, `priorWd`
- Boundary flags: `is_new_policy`, `is_new_day`, `is_new_transaction`, `is_new_fund`
- Timing: `t`, `dt`, `bop_date`, `eop_date`, `eot_date`, `pol_yr`, `remaining_time_pct`, `crediting_now`, `deposit_ct`, `is_relocation`, `is_anniversary`, `is_terminated`
- Fund parameters: `cap`, `buffer`, `par`, `trig`, `lock`, `fixed_rate`, `upside_param`, `alt_fund_code`, `index_name`
- Index/OUV: `index_value`, `index_return`, `bop_ouv`, `eop_ouv`, `trading_cost`
- Results: `strategy_base`, `strategy_return`, `av_pre_txn`, `interest_credit_amt`, `av_end_txn`, `mwv_interim`, `mwv_base`, `surr_charge`, `fund_gross_wdl`, `fund_net_wdl`, `pol_fwv`, `fund_attribution`
- Lock tracking: `auto_locked`, `manual_locked`, `lock_credited`, `AS_txn_amt`
- Aggregates: `total_fund_value`
- System comparison: `sys_av_c`, `diff_av`, `sys_mwv_c`, `diff_mwv`

---

### 2b. `RollForward_policy.get_rollforward_from_extract()` — Policy-Level Rollforward Engine

**Granularity:** `POLICY_NUM` only — operates at the policy level (no fund dimension).

**Execution order:** Runs **after** `RollForward_base.py`. Takes the fund-aggregated AV from step 8 above as input.

**Inputs:**


| Parameter          | Type           | Description                                                                                                                                                                                                                                                                                                                              |
| ------------------ | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data_long`        | `pd.DataFrame` | The`data_by_txn` DataFrame from `RollForward_base`, already enriched with `av_end_txn` and `mwv_interim` columns (merged in step 8). Includes `POLICY_NUM`, `TRANSACTION_NUM`, `EFFECTIVE_DATE`, `TRANSACTION_DESC`, `av_end_txn`, `mwv_interim`, `GrWd+PErecap`, `ACCOUNT_VAL`, `DB_OPTION`, `PL_PRD_DESC`, and all policy-level fields |
| `results_basepath` | `str`          | Same output directory                                                                                                                                                                                                                                                                                                                    |
| `surr_file_path`   | `str`          | Same surrender charge table path                                                                                                                                                                                                                                                                                                         |
| `fund_mapping`     | `dict`         | Uses`FUND_INDEX_MAP` and `FUND_EQINDEX_MAP` only                                                                                                                                                                                                                                                                                         |
| `pols_dict`        | `dict`         | The nested dict built by`RollForward_base`, passed in to be enriched with policy-level fields                                                                                                                                                                                                                                            |

**Processing Steps:**

1. **Load surrender charge dict** — reuses `RollForward_base.load_surr_chrg_table_to_dict()`.
2. **Main loop** — iterates each row, maintaining `states: Dict[str, GMDBState]` keyed by policy number only (no fund dimension):


   | Sub-step                  | Purpose                                                                                                                                                                                                                                      |
   | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `get_trxn_amount()`       | Classifies Deposit/Withdrawal/Fee at policy level. Uses`"Withdrawal"` only (not the broader `WDL_TYPES` in base). Reset types are `{"Deposit", "Reallocation Transfer"}`                                                                     |
   | `calculate_timing()`      | Tracks**2nd anniversary logic** (unique to policy-level). Advance by 2 years after each reset/anniversary. Also tracks deposit count and boundary flags                                                                                      |
   | `calculate_t()`           | Simpler time calculation — no`remaining_time_pct`, `eot_date`, or `bot_date` (those are fund-specific)                                                                                                                                      |
   | `calculate_net_premium()` | Tracks net premium, which is the initial premium reduced proportionally by withdrawals:`net_premium = prior_net_premium * av_end_txn / (av_end_txn - gross_withdrawal)`                                                                      |
   | `calculate_fee()`         | Computes**ROP (Return of Premium) Death Benefit Fee**: `0.002 * net_premium * elapsed_pct_of_current_policy_year`. Charged fully on `Index Valuation` or `ROP Death Benefit Fee` txns; otherwise prorated by days elapsed in the policy year |
   | `get_surrender_chrg()`    | Same surrender charge lookup as base engine                                                                                                                                                                                                  |
   | `calculate_csv()`         | Computes Cash Surrender Value and Death Benefit (see formula below)                                                                                                                                                                          |

   **CSV and Death Benefit formulas:**


   ```
   fwv (Free Withdrawal Value) = 10% of av_eop, replenished annually, reduced by withdrawals
   surr_chrg = max(0, av_eop - fwv) * surr_rate / 100
   csv = max(mwv, av_eop - surr_chrg - fee)
   death_ben = max(av_eop - fee, mwv, net_premium)
   ```
3. **Enrich `pols_dict`** — after computing `csv` and `death_ben` for each row:

   - Calls `add_pol_level_info()` to attach these values to the matching transaction in the nested dict
   - Calls `add_txn_level_info()` to sum `interest_credited` across all funds within each transaction

**Outputs:**


| Output                               | Type   | Description                                                                                                                   |
| ------------------------------------ | ------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `pols_dict` (enriched)               | `dict` | Same nested structure, now with policy-level`csv`, `death_ben`, and transaction-level `interest_credited` summed across funds |
| `4. Data_Pol_Level_wRollforward.csv` | File   | Policy-level rollforward results with all computed columns                                                                    |

**Key computed columns** (appended to input DataFrame):

- Transaction classification: `Fee`, `Wd`, `accum_wd`, `has_withdrawal_taken`, `priorWd`, `accum_prem`
- Boundary flags: `is_new_policy`, `is_new_day`, `is_new_transaction`, `is_new_fund`, `has_first_deposit`, `has_second_deposit`, `deposit_ct`, `is_relocation`, `is_anniversary`, `2nd_anniversary`, `is_2nd_anniversary`
- Timing: `t`, `dt`, `bop_date`, `eop_date`, `pol_yr`
- Guarantees: `mwv`, `surr_rate`, `Net_prem`, `ROP fee`, `fwd_amt`, `surr_chrg`, `csv`, `death_ben`

---

### Relationship Between the Two Rollforward Engines

**Execution order:** `RollForward_base.py` → `RollForward_policy.py`

**Data flow:**

```
data_by_fund (fund-granular transactions)
    │
    ▼
RollForward_base.get_rollforward_from_extract()
    │
    ├── Per-fund calculations:
    │   ├── Strategy-specific returns (buffer, cap, lock, trigger, etc.)
    │   ├── OUV-based interim valuations
    │   ├── VIX-based trading costs
    │   ├── Index tracking and max-index tracking (for lock funds)
    │   ├── Per-fund AV and MWV rollforwards
    │   └── Surrender charges (pro-rata by fund portion)
    │
    ├── Aggregates av_end_txn + mwv_interim by (POLICY_NUM, TRANSACTION_NUM)
    ├── Merges aggregated values into data_by_txn
    │
    ├──→ Returns: (rollforward_results_df, pols_dict)
    └──→ Writes: 2. Data_Txn_Level_wRollforward.csv, 3. Data_Pol_Level_wAV.csv

3. Data_Pol_Level_wAV.csv (or in-memory data_full_with_av)
    │
    ▼
RollForward_policy.get_rollforward_from_extract()
    │
    ├── Policy-level calculations (uses av_end_txn as input):
    │   ├── 2nd anniversary tracking
    │   ├── Net premium (initial premium reduced proportionally by withdrawals)
    │   ├── ROP Death Benefit Fee accrual
    │   ├── Cash Surrender Value (AV - surrender charge - fee, floored at MWV)
    │   └── Death Benefit (max of AV-fee, MWV, or net premium)
    │
    ├── Enriches pols_dict with csv, death_ben, and summed interest_credited
    │
    ├──→ Returns: enriched pols_dict
    └──→ Writes: 4. Data_Pol_Level_wRollforward.csv
```

**Architectural distinction:**


| Aspect                 | `RollForward_base.py`                                                                         | `RollForward_policy.py`                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Granularity            | `(policy, fund)` — fund-level                                                                | `policy` — policy-level only                                                     |
| Primary responsibility | Fund-level actuarial calculations: strategy returns, OUV, index tracking, AV/MWV rollforwards | Policy-level guarantees: CSV, Death Benefit, ROP Fee, Net Premium                 |
| State size             | ~70 fields (index values, OUV, strat base, fund portion, lock flags, etc.)                    | ~40 fields (2nd anniversary, net premium, cumulative premiums, CSV/death benefit) |
| Output focus           | `av_end_txn`, `mwv_interim`, `strategy_return` per fund                                       | `csv`, `death_ben`, `ROP fee`, `Net_prem` per policy                              |

### Outputs

- **`pols_structure.json`** — Nested dict: `policy → days → transactions → funds` with all calculated values
- **Intermediate CSVs** — `4. Data_Pol_Level_wRollforward.csv`, rollforward results, etc.

---

## 3. `post_process.py` — Summary Generation & Difference Detection

**Purpose:** Reads `pols_structure.json` and generates human-readable summaries and automated difference detection.

### Key Outputs


| Output                            | Description                                                                                                                  |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `general_summary`                 | Total policy count, GMDB policy count                                                                                        |
| `policy_summary.csv`              | Per-policy: txn type counts (deposits, fees, withdrawals, reallocations, anniversaries), BOP/EOP fund counts, summed amounts |
| `fund_summary_{policy}.csv`       | Per-policy fund-level: AV, MWV, sys_AV, sys_MWV with differences                                                             |
| `fund_difference_list.csv/.txt`   | Policies where `                                                                                                             |
| `policy_difference_list.csv/.txt` | Policies where AV/MWV/CSV/DB diff > $10 (compares against`4. Data_Pol_Level_wRollforward.csv`)                               |
| `post_process_summary.txt`        | Single consolidated text file with all summaries                                                                             |

**Returns:** `(total_policy_not_pass, policy_difference_list, general_summary)` — consumed by the email module

---

## 4. `compare_program.py` — Program-vs-System Comparison

**Purpose:** Compares program-calculated values against the system-generated actuarial statement text file.

### Key Steps

1. **Copy system text file** — If "Statement Run" flag is set in UI Excel, copies `Actuarial_Extract_*.txt` from a network share into `results_basepath`
2. **Filter by policy** — `_filter_sys_txt_by_policies()` filters the text file to only lines matching policies in `pols_structure.json`
3. **Extract program values** — Three extraction methods from `policy_structure.Policy`:
   - `extract_pol_level_items()` — Policy-level AS items
   - `extract_fund_level_items()` — Fund-level AS items
   - `extract_txn_fund_level_items()` — Transaction-fund-level AS items
4. **Parse system text file** — Three fixed-width parsers:
   - `_parse_sys_pol_level(lines)` — Parses `QQTRDATESREC`, `QACCTSUMMREC*`, `QACCTOPTDBRE*` records
   - `_parse_sys_fund_level(lines)` — Parses `QALLOCDETREC`, `QCNTRTVALREC*`, `QPERFSUMMREC` records
   - `_parse_sys_txn_fund_level(lines)` — Parses `QTRANSUMMRECH*` / `QTRANSUMMRECD*` header-detail pairs
5. **Generate comparison Excel files** — Three functions produce Excel files with `cmp_*` boolean columns, `diff_*` numeric columns, and `cmp_all_match` aggregate:
   - `comp_pol_level_items.xlsx` — Policy-level comparison
   - `comp_fund_level_items.xlsx` — Fund-level comparison
   - `comp_txn_fund_level_items.xlsx` — Transaction-fund-level comparison

**Returns:** Six DataFrames — 3 program-extracted + 3 system-parsed

---

## 5. `logger.py` — Logging Infrastructure


| Function                       | Purpose                                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------- |
| `setup_root_logging(log_dir)`  | Configures root logger with console + file handlers. Creates`validation_YYYYMMDD_HHMMSS.log` |
| `create_logger(name, log_dir)` | Creates named module-level loggers with own handlers. Creates`{name}_YYYY-MM-DD_HHMMSS.log`  |

---

## 6. `demo_email.py` — Email Notification

**Purpose:** Sends email notification with run results (currently disabled in `Main_run_validation_process.py`).

**Key Features:**

- Wraps a corporate `EmailTool.exe` on a network share (`\\pnsafsda28\mrm_root`)
- Composes email body with: run timestamps, elapsed time, input file names, general summary table, list of failing policies, suggested debug files
- Saves body to `email_summary.txt`
- Invokes `EmailTool.exe` via `subprocess.run` with 60-second timeout

---

## Data Flow Summary

```
Input_RILA/
├── RILA_Validation_UI_v1_Step1.xlsx   → User config, fund mappings
└── RILA_SC.csv                        → Surrender values

validation_process.py
├── Navisys DB → raw CSVs
├── Unpivot wide → long format
├── Fund-level rollforward
├── Policy-level rollforward
└── → pols_structure.json + intermediate CSVs

post_process.py
├── Reads pols_structure.json
├── Builds summaries + difference lists
└── → post_process_summary.txt, *_difference_list.csv, policy_summary.csv

compare_program.py
├── Reads pols_structure.json + system .txt
├── Extracts program values + parses system values
├── Filters system file to matching policies
└── → comp_*_items.xlsx (3 files with conditional formatting)

demo_email.py (disabled)
└── → Email with results summary
```

---

## `policy_structure/` Submodule

Located at `c:\Users\Leon\Desktop\RILA_Python_Project\Code\policy_structure\`


| File                   | Purpose                                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `Policy.py`            | `Policy` dataclass + methods for extracting AS items at policy, fund, and txn-fund levels from `pols_structure.json` |
| `Transaction.py`       | `Transaction` dataclass + fund aggregation methods                                                                   |
| `Funds.py`             | `Funds` dataclass with fund-level fields (av, mwv, strat_base, buffer, upside params, etc.)                          |
| `Role.py`              | Lightweight role/owner dataclass for WB flow                                                                         |
| `convert_to_txt.py`    | Converts`.py` files → `.txt` files                                                                                  |
| `convert_txt_to_py.py` | Renames`.txt` files → `.py` via `os.rename()`                                                                       |

---

*Generated from codebase analysis of `c:\Users\Leon\Desktop\RILA_Python_Project\Code\`*
