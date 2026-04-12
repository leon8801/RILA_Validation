# compare_program.py
# ruff: noqa: F541
import os
import json
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
import pandas as pd
from pandas.tseries.offsets import BDay
import logging
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from validation_process import read_excel_user_inputs
_logger = logging.getLogger(__name__)


# ---------- System text file field mapping ----------
# Each entry: (output_key, prefix, start_pos_1based, length)
# Positions are absolute from start of line (not from prefix).

def _clean_number(raw: str) -> Optional[float]:
    """Strip, remove $, commas, %, convert to float. Returns None on failure."""
    raw = raw.strip().replace("$", "").replace(",", "").replace("%", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _format_percentage(raw: str) -> Optional[str]:
    """Convert raw string to percentage format like '00.00%'."""
    num = _clean_number(raw)
    if num is None:
        return None
    return f"{num:06.2f}%"


def _filter_sys_txt_by_policies(file_path: str, policy_nums: set) -> List[str]:
    """
    Read system text file and return only lines whose policy number (after leading 'SCO')
    matches one of the provided policy_nums.
    """
    filtered: List[str] = []
    with open(file_path, "r", encoding="cp1252") as f:
        for line in f:
            stripped = line.rstrip(" ")
            if not stripped:
                continue
            # Extract policy number: strip leading "SCO", take first token
            if stripped.startswith("SCO"):
                pol = stripped[3:].strip().split()[0]
            else:
                continue
            if pol in policy_nums:
                filtered.append(stripped)
    return filtered


def _parse_sys_pol_level(lines: List[str]) -> pd.DataFrame:
    """
    Parse the system actuarial text file and extract policy-level items.

    Mapping format: (output_key, prefix, start_pos_1based, length)
    Positions are absolute from start of line.
    Policy boundaries are detected by QQTRDATESREC lines.

    Returns a DataFrame with one row per policy block.
    """
    SYS_POL_FIELD_MAP = [
    ("av_prev_ann_contsummary",         "QQTRDATESREC", 111, 15),
    ("av_curr_ann_contsummary",         "QQTRDATESREC", 149, 15),
    ("prem_since_issue_contsummary",    "QACCTSUMMREC001", 80, 25),
    ("prem_this_period_contsummary",    "QACCTSUMMREC001", 105, 25),
    ("withdrawals_since_issue",         "QACCTSUMMREC002", 80, 25),
    ("withdrawals_this_period",         "QACCTSUMMREC002", 105, 25),
    ("interest_since_issue",            "QACCTSUMMREC003", 80, 25),
    ("interest_this_period",            "QACCTSUMMREC003", 105, 25),
    ("cmv_since_issue",                 "QACCTSUMMREC004", 80, 25),
    ("cmv_this_period",                 "QACCTSUMMREC004", 105, 25),
    ("account_value_impinfo",           "QACCTOPTDBRE001", 80, 25),
    ("minwdl_value_impinfo",            "QACCTOPTDBRE002", 80, 25),
    ("cash_value_impinfo",              "QACCTOPTDBRE003", 80, 25),
    ("death_benefit_impinfo",           "QACCTOPTDBRE004", 80, 25),
    ]
    # Group map entries by prefix
    prefix_fields: Dict[str, List[Tuple[str, int, int]]] = {}
    for key, prefix, start, length in SYS_POL_FIELD_MAP:
        prefix_fields.setdefault(prefix, []).append((key, start, length))

    all_prefixes = list(prefix_fields.keys())

    records: List[Dict[str, Any]] = []
    current_record: Dict[str, Any] = {}

    for stripped in lines:
        # Extract policy number from start of line (remove leading "SCO")
        policy_num_raw = stripped[:15] if len(stripped) >= 15 else ""
        policy_num = policy_num_raw[3:].strip() if policy_num_raw.startswith("SCO") else policy_num_raw.strip()

        # Find which prefix this line matches (search, not startswith)
        matched_prefix = None
        for prefix in all_prefixes:
            if prefix in stripped:
                matched_prefix = prefix
                break

        if matched_prefix is None:
            continue

        # Store policy number in the current record
        current_record['policy_num'] = policy_num

        # Extract fields for this prefix
        for key, start, length in prefix_fields[matched_prefix]:
            idx = start - 1  # convert 1-based to 0-based
            raw = stripped[idx: idx + length].strip()
            current_record[key] = _clean_number(raw)

        # QQTRDATESREC marks the end of a policy block — flush after extraction
        if matched_prefix == "QQTRDATESREC" and current_record:
            records.append(current_record)
            current_record = {}

    # flush last record
    if current_record:
        records.append(current_record)

    records = pd.DataFrame(records)
    records = records[['policy_num', 'av_prev_ann_contsummary','av_curr_ann_contsummary',
                       	'prem_since_issue_contsummary','prem_this_period_contsummary','withdrawals_since_issue',
                        'withdrawals_this_period','interest_since_issue','interest_this_period','cmv_since_issue',
                        'cmv_this_period','account_value_impinfo','minwdl_value_impinfo','cash_value_impinfo','death_benefit_impinfo']]

    return records


def _parse_sys_fund_level(lines: List[str]) -> pd.DataFrame:
    """
    Parse the system actuarial text file and extract fund-level items.

    Mapping format: (output_key, prefix, start_pos_1based, length)
    Positions are absolute from start of line.

    The file groups records by type (all QALLOCDETREC first, then all
    QCNTRTVALREC*, then all QPERFSUMMREC). All record types for the same
    fund are merged into a single row. Policy number is extracted from
    each line (removing leading "SCO" prefix).

    Returns a DataFrame with one row per fund.
    """
    SYS_FUND_FIELD_MAP = [
        # Allocation Details
        ("alloc_fund_name",      "QALLOCDETREC",  41,  50),
        ("alloc_term_start",     "QALLOCDETREC",  91,  20),
        ("alloc_term_end",       "QALLOCDETREC",  111, 20),
        ("alloc_contract_value", "QALLOCDETREC",  131, 25),
        ("alloc_pct",            "QALLOCDETREC",  156, 15),
        ("alloc_buffer",         "QALLOCDETREC",  171, 15),
        ("alloc_upside_type",    "QALLOCDETREC",  186, 25),
        ("alloc_upside_rate",    "QALLOCDETREC",  211, 25),
        # Contract Value - Locked Funds
        ("locked_fund_name",     "QCNTRTVALRECL", 42,  50),
        ("av_locked",            "QCNTRTVALRECL", 92,  25),
        ("lock_fixed_rate",      "QCNTRTVALRECL", 117, 15),
        ("int_credited_fixed",   "QCNTRTVALRECL", 132, 25),
        ("av_locked_plus_int",   "QCNTRTVALRECL", 157, 25),
        # Contract Value - Mid Term Funds
        ("mid_fund_name",        "QCNTRTVALRECM", 42,  50),
        ("mid_strat_base",       "QCNTRTVALRECM", 92,  25),
        ("mid_av",               "QCNTRTVALRECM", 157, 25),
        # Contract Value - End of Term Funds
        ("eot_fund_name",        "QCNTRTVALRECE", 42,  50),
        ("eot_strat_base",       "QCNTRTVALRECE", 92,  25),
        ("eot_index_credited",   "QCNTRTVALRECE", 117, 15),
        ("eot_interest_credited","QCNTRTVALRECE", 132, 25),
        ("eot_account_value",    "QCNTRTVALRECE", 157, 25),
        # Performance Summary
        ("perf_fund_name",       "QPERFSUMMREC",  41,  50),
        ("perf_term_start",      "QPERFSUMMREC",  91,  20),
        ("perf_term_end",        "QPERFSUMMREC",  111, 20),
        ("perf_beg_index_val",   "QPERFSUMMREC",  131, 20),
        ("perf_end_index_val",   "QPERFSUMMREC",  151, 20),
        ("perf_raw_index_chg",   "QPERFSUMMREC",  171, 15),
        ("perf_upside_rate",     "QPERFSUMMREC",  186, 25),
        ("perf_downside_buffer", "QPERFSUMMREC",  211, 15),
        ("perf_index_credited",  "QPERFSUMMREC",  226, 15),
    ]

    # Map each prefix to its fund-name field key
    FUND_NAME_FIELDS = {
        "QALLOCDETREC": "alloc_fund_name",
        "QCNTRTVALRECL": "locked_fund_name",
        "QCNTRTVALRECM": "mid_fund_name",
        "QCNTRTVALRECE": "eot_fund_name",
        "QPERFSUMMREC": "perf_fund_name",
    }

    STRING_FIELDS = {
        "alloc_fund_name", "alloc_term_start", "alloc_term_end",
        "alloc_upside_type", "locked_fund_name", "lock_fixed_rate",
        "mid_fund_name", "eot_fund_name", "perf_fund_name",
        "perf_term_start", "perf_term_end", "perf_upside_rate",
        "perf_downside_buffer",
    }

    PERCENTAGE_FIELDS = {
        "perf_raw_index_chg", "perf_upside_rate", 
        "perf_downside_buffer", "perf_index_credited",
    }

    prefix_fields: Dict[str, List[Tuple[str, int, int]]] = {}
    for key, prefix, start, length in SYS_FUND_FIELD_MAP:
        prefix_fields.setdefault(prefix, []).append((key, start, length))

    all_prefixes = list(prefix_fields.keys())

    # Accumulate records keyed by normalized fund name
    fund_records: Dict[str, Dict[str, Any]] = {}

    # Collect all field keys so every fund row has the same columns
    all_field_keys = [key for key, _, _, _ in SYS_FUND_FIELD_MAP]

    for stripped in lines:
        # Extract policy number from line (remove leading "SCO")
        policy_num = None
        if stripped.startswith("SCO"):
            policy_num = stripped[3:].split()[0] if len(stripped) > 3 else None

        matched_prefix = None
        for prefix in all_prefixes:
            if prefix in stripped:
                matched_prefix = prefix
                break

        if matched_prefix is None:
            continue

        # Determine fund name key from this line
        fname_field = FUND_NAME_FIELDS.get(matched_prefix)
        fund_name_val = None
        if fname_field:
            for key, start, length in prefix_fields[matched_prefix]:
                if key == fname_field:
                    idx = start - 1
                    raw = stripped[idx: idx + length].strip() if idx < len(stripped) else ""
                    fund_name_val = raw if raw else None
                    break

        # Skip lines with no fund name
        if not fund_name_val:
            continue

        # Initialize new fund record with all fields set to None
        rec = fund_records.setdefault(fund_name_val, {k: None for k in all_field_keys})

        # Store policy_num if extracted
        if policy_num is not None:
            rec["_policy_num"] = policy_num

        # Extract all fields for this prefix
        for key, start, length in prefix_fields[matched_prefix]:
            idx = start - 1
            raw = stripped[idx: idx + length].strip() if idx < len(stripped) else ""
            if key in PERCENTAGE_FIELDS:
                val = _format_percentage(raw)
            elif key in STRING_FIELDS:
                val = raw if raw else None
            else:
                val = _clean_number(raw)
            # Only set if not already populated
            if rec.get(key) is None:
                rec[key] = val

    records = [dict(rec) for rec in fund_records.values()]
    df = pd.DataFrame(records)
    
    # Move policy_num to first column if present
    if "_policy_num" in df.columns:
        df.rename(columns={"_policy_num": "policy_num"}, inplace=True)
        cols = df.columns.tolist()
        cols = ["policy_num"] + [col for col in cols if col != "policy_num"]
        df = df[cols]
    
    return df


def _export_pol_comparison_to_excel(pol_items_df: pd.DataFrame, sys_pol_df: pd.DataFrame, results_basepath: str) -> None:
    """
    Merge program and system policy-level DataFrames, build comparison columns,
    and export to Excel.

    Join key: policy_num. All other fields are numeric → diff_ columns computed.
    Adds cmp_all_match boolean column (True if all diffs ≈ 0).
    """
    if pol_items_df.empty or sys_pol_df.empty:
        return

    # Ensure one row per policy_num before merging
    prog_df = pol_items_df.drop_duplicates(subset=["policy_num"]).copy()
    sys_df = sys_pol_df.drop_duplicates(subset=["policy_num"]).copy()

    # Rename columns to distinguish program from system
    prog_df.rename(columns={col: f"prog_{col}" for col in prog_df.columns if col != "policy_num"}, inplace=True)
    sys_df.rename(columns={col: f"sys_{col}" for col in sys_df.columns if col != "policy_num"}, inplace=True)

    # Merge on policy_num
    comp_df = pd.merge(prog_df, sys_df, on="policy_num", how="outer")

    # Create comparison columns: round both sides to 2 decimals, check equality
    prog_cols = [col for col in comp_df.columns if col.startswith("prog_")]
    for prog_col in prog_cols:
        base_col = prog_col[5:]
        sys_col = f"sys_{base_col}"
        if sys_col in comp_df.columns:
            comp_df[f"cmp_{base_col}"] = (
                comp_df[prog_col].round(2) == comp_df[sys_col].round(2)
            )

    # Aggregate match column (AND of all cmp_ columns)
    cmp_cols = [col for col in comp_df.columns if col.startswith("cmp_")]
    if cmp_cols:
        comp_df["cmp_all_match"] = comp_df[cmp_cols].all(axis=1)

    # Move cmp_all_match to first column
    cols = ["cmp_all_match"] + [c for c in comp_df.columns if c != "cmp_all_match"]
    comp_df = comp_df[cols]

    # Export to Excel
    excel_path = os.path.join(results_basepath, "comp_pol_level_items.xlsx")
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        comp_df.to_excel(writer, index=False, sheet_name="Comparisons")


def _export_fund_comparison_to_excel(fund_items_df: pd.DataFrame, sys_fund_df: pd.DataFrame, results_basepath: str) -> None:
    """
    Merge program and system fund-level DataFrames, build comparison columns,
    and export to Excel with conditional formatting.

    Steps:
    - Outer-join on perf_fund_name, prefix columns with prog_/sys_
    - Normalize dates to YYYY-MM-DD and percentages via _format_percentage
    - Create cmp_ columns (string equality) and diff_ columns (numeric subtraction)
    - Add cmp_all_match aggregate (AND of all cmp_ columns)
    - Export to Excel with yellow highlighting on FALSE cmp_ values

    Args:
        fund_items_df: Program-calculated fund-level DataFrame
        sys_fund_df: System-parsed fund-level DataFrame
        results_basepath: Directory where comp_fund_level_items.xlsx will be saved
    """
    if fund_items_df.empty or sys_fund_df.empty:
        return

    prog_df = fund_items_df.copy()
    sys_df = sys_fund_df.copy()

    # Create join key before renaming
    prog_df['_join_key'] = prog_df['perf_fund_name']
    sys_df['_join_key'] = sys_df['perf_fund_name']

    # Rename columns to distinguish program from system
    prog_df.rename(columns={col: f"prog_{col}" for col in prog_df.columns if col != "_join_key"}, inplace=True)
    sys_df.rename(columns={col: f"sys_{col}" for col in sys_df.columns if col != "_join_key"}, inplace=True)

    # Merge on join key to keep both fund names
    comp_df = pd.merge(prog_df, sys_df, on="_join_key", how="outer")
    comp_df.drop(columns=["_join_key"], inplace=True)

    # Normalize date and percentage fields before comparison
    date_fields = ["alloc_term_start", "alloc_term_end", 'perf_term_start', 'perf_term_end']
    percentage_fields = ["alloc_pct", "alloc_buffer", "eot_index_credited"]

    for field in date_fields:
        for prefix in ("prog_", "sys_"):
            col = f"{prefix}{field}"
            if col in comp_df.columns:
                comp_df[col] = pd.to_datetime(comp_df[col], errors='coerce').dt.strftime('%Y-%m-%d')

    for field in percentage_fields:
        for prefix in ("prog_", "sys_"):
            col = f"{prefix}{field}"
            if col in comp_df.columns:
                comp_df[col] = comp_df[col].apply(
                    lambda x: _format_percentage(str(x)) if x is not None else None
                )

    # Create comparison columns for each prog/sys pair
    prog_cols = [col for col in comp_df.columns if col.startswith("prog_")]
    for prog_col in prog_cols:
        base_col = prog_col[5:]  # Remove "prog_" prefix
        sys_col = f"sys_{base_col}"
        if sys_col not in comp_df.columns:
            continue

        prog_vals = comp_df[prog_col]
        sys_vals = comp_df[sys_col]

        if comp_df[prog_col].dtype == "object" or comp_df[sys_col].dtype == "object":
            # String comparison (case-insensitive)
            comp_df[f"cmp_{base_col}"] = prog_vals.astype(str).str.lower() == sys_vals.astype(str).str.lower()
        else:
            # Numeric comparison (subtraction)
            comp_df[f"diff_{base_col}"] = prog_vals - sys_vals

    # Collect all cmp column names for aggregate calculation
    cmp_cols = [col for col in comp_df.columns if col.startswith('cmp_')]
    if cmp_cols:
        comp_df['cmp_all_match'] = comp_df[cmp_cols].all(axis=1)

    # Move cmp_all_match to first column
    cols = ["cmp_all_match"] + [c for c in comp_df.columns if c != "cmp_all_match"]
    comp_df = comp_df[cols]

    # Export to Excel with conditional formatting for FALSE values
    excel_path = os.path.join(results_basepath, "comp_fund_level_items.xlsx")
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        comp_df.to_excel(writer, index=False, sheet_name='Comparisons')

        worksheet = writer.sheets['Comparisons']
        yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

        for col_idx, col in enumerate(comp_df.columns, start=1):
            if col.startswith('cmp_'):
                col_letter = get_column_letter(col_idx)
                for row_idx in range(2, len(comp_df) + 2):
                    cell = worksheet[f'{col_letter}{row_idx}']
                    if cell.value is False:
                        cell.fill = yellow_fill

def _export_txn_fund_comparison_to_excel(txn_items_df: pd.DataFrame, sys_txn_df: pd.DataFrame, results_basepath: str) -> None:
    """
    Merge program and system transaction-fund-level DataFrames, build comparison columns,
    and export to Excel with conditional formatting.

    Join key: (txn_date, txn_type_desc, fund_name)

    Args:
        txn_items_df: Program-calculated txn-fund-level DataFrame
        sys_txn_df: System-parsed txn-fund-level DataFrame
        results_basepath: Directory where comp_txn_fund_level_items.xlsx will be saved
    """
    if txn_items_df.empty or sys_txn_df.empty:
        return

    prog_df = txn_items_df.copy()
    sys_df = sys_txn_df.copy()

    # Convert date fields to datetime objects for proper chronological sorting
    for df in (prog_df, sys_df):
        if "txn_date" in df.columns:
            df["txn_date"] = pd.to_datetime(df["txn_date"], errors="coerce")
        if "term_start_date" in df.columns:
            df["term_start_date"] = pd.to_datetime(df["term_start_date"], errors="coerce")

    # Sort by policy_num, txn_date, term_start_date, txn_type_desc, fund_name
    sort_cols = [c for c in ['policy_num', 'txn_date', 'term_start_date', 'txn_type_desc', 'fund_name'] if c in prog_df.columns]
    prog_df.sort_values(by=sort_cols, inplace=True)
    sys_df.sort_values(by=sort_cols, inplace=True)

    # Normalize dates to YYYY-MM-DD strings for join key and comparison
    date_fields = ["txn_date", "term_start_date", "term_end_date"]
    for df in (prog_df, sys_df):
        for field in date_fields:
            if field in df.columns:
                df[field] = pd.to_datetime(df[field], errors="coerce").dt.strftime("%Y-%m-%d")

    # Create composite join key BEFORE renaming
    prog_df["_join_key"] = (
        prog_df["txn_date"].fillna("") + "|" +
        prog_df["term_start_date"].fillna("") + "|" +
        prog_df["txn_type_desc"].fillna("") + "|" +
        prog_df["fund_name"].fillna("")
    )
    sys_df["_join_key"] = (
        sys_df["txn_date"].fillna("") + "|" +
        sys_df["term_start_date"].fillna("") + "|" +
        sys_df["txn_type_desc"].fillna("") + "|" +
        sys_df["fund_name"].fillna("")
    )

    # Rename columns to distinguish program from system
    prog_df.rename(columns={col: f"prog_{col}" for col in prog_df.columns if col != "_join_key"}, inplace=True)
    sys_df.rename(columns={col: f"sys_{col}" for col in sys_df.columns if col != "_join_key"}, inplace=True)

    # Merge on join key
    comp_df = pd.merge(prog_df, sys_df, on="_join_key", how="outer")
    comp_df.drop(columns=["_join_key"], inplace=True)

    # Create comparison columns
    prog_cols = [col for col in comp_df.columns if col.startswith("prog_")]
    for prog_col in prog_cols:
        base_col = prog_col[5:]
        sys_col = f"sys_{base_col}"
        if sys_col not in comp_df.columns:
            continue

        prog_vals = comp_df[prog_col]
        sys_vals = comp_df[sys_col]

        if comp_df[prog_col].dtype == "object" or comp_df[sys_col].dtype == "object":
            comp_df[f"cmp_{base_col}"] = prog_vals.astype(str).str.lower() == sys_vals.astype(str).str.lower()
        else:
            comp_df[f"diff_{base_col}"] = prog_vals - sys_vals

    # Aggregate column
    cmp_cols = [col for col in comp_df.columns if col.startswith('cmp_')]
    if cmp_cols:
        comp_df['cmp_all_match'] = comp_df[cmp_cols].all(axis=1)

    # Move cmp_all_match to first column
    cols = ["cmp_all_match"] + [c for c in comp_df.columns if c != "cmp_all_match"]
    comp_df = comp_df[cols]

    # Export to Excel
    excel_path = os.path.join(results_basepath, "comp_txn_fund_level_items.xlsx")
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        comp_df.to_excel(writer, index=False, sheet_name='Comparisons')

        worksheet = writer.sheets['Comparisons']
        yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

        for col_idx, col in enumerate(comp_df.columns, start=1):
            if col.startswith('cmp_'):
                col_letter = get_column_letter(col_idx)
                for row_idx in range(2, len(comp_df) + 2):
                    cell = worksheet[f'{col_letter}{row_idx}']
                    if cell.value is False:
                        cell.fill = yellow_fill


def _parse_sys_txn_fund_level(lines: List[str]) -> pd.DataFrame:
    """
    Parse the system actuarial text file and extract transaction-fund-level items.

    Transaction records consist of:
      - QTRANSUMMRECH* header line(s): policy_num, txn_date, txn_type_desc
      - QTRANSUMMRECD* detail line(s): fund_name, term_start, term_end, amount

    Each (header + detail) pair produces one row.

    Returns a DataFrame with one row per fund per transaction, with policy_num as first field.
    """
    records: List[Dict[str, Any]] = []
    current_header: Dict[str, Any] = {}
    header_prefix = "QTRANSUMMRECH"
    detail_prefix = "QTRANSUMMRECD"

    for stripped in lines:
        if header_prefix in stripped:
            # New transaction header — extract policy number and transaction details
            current_header = {}
            # Extract policy_num (first token before spaces), remove leading "SCO"
            pol_num = stripped.split()[0] if stripped.split() else ""
            if pol_num.startswith("SCO"):
                pol_num = pol_num[3:]
            current_header["policy_num"] = pol_num if pol_num else None
            # Extract txn_date (pos 37, len 15)
            raw_date = stripped[36:36+15].strip() if len(stripped) >= 51 else ""
            current_header["txn_date"] = raw_date if raw_date else None
            # Extract txn_type_desc (pos 52, len 50)
            raw_desc = stripped[51:51+50].strip() if len(stripped) >= 101 else ""
            raw_desc = raw_desc.replace(":", "")
            raw_desc = raw_desc.replace("*", "")
            current_header["txn_type_desc"] = raw_desc if raw_desc else None

        elif detail_prefix in stripped:
            # Detail line — one row per fund
            if not current_header:
                continue

            # Extract fund_name (pos 47, len 50)
            raw_fname = stripped[46:46+50].strip() if len(stripped) >= 96 else ""
            fund_name = raw_fname if raw_fname else None

            # Extract term_start (pos 162, len 25)
            raw_tstart = stripped[161:161+25].strip() if len(stripped) >= 186 else ""
            term_start = raw_tstart if raw_tstart else None

            # Extract term_end (pos 187, len 25)
            raw_tend = stripped[186:186+25].strip() if len(stripped) >= 211 else ""
            term_end = raw_tend if raw_tend else None

            # Extract amount (pos 97, len 25)
            raw_amt = stripped[96:96+25].strip() if len(stripped) >= 121 else ""
            amount = _clean_number(raw_amt)

            records.append({
                "policy_num": current_header.get("policy_num"),
                "txn_date": current_header.get("txn_date"),
                "txn_type_desc": current_header.get("txn_type_desc"),
                "fund_name": fund_name,
                "term_start_date": term_start,
                "term_end_date": term_end,
                "txn_amount": amount,
            })

    return pd.DataFrame(records)


# ----------------------------
# Main entry point
# ----------------------------

def run_compare_process(results_basepath: str, UI_file_path: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Read pols_structure.json in results_basepath and extract Annual Statement items one by one
    Then extract the same AS items from printing team's text file (sysfile).

    Args:
        results_basepath: Directory where outputs will be saved and pols_structure.json is expected.
        sys_txt_src_dir: Optional directory containing the system actuarial text file.
                         If provided, the file will be copied into results_basepath.
    """
    os.makedirs(results_basepath, exist_ok=True)

    df_run_option, _ = read_excel_user_inputs(UI_file_path)
    statement_run_bool = df_run_option["Statement Run"].iloc[0]
    statement_date = df_run_option["Cycle Date"].iloc[0] + BDay(1)
    statement_date = statement_date.strftime('%Y%m%d')
    sys_txt_src_dir = r"\\PWASCO000751001.msad.crbgf.net\RAStatements\Actuarial\\" + statement_date + "\\"
    
    # Copy system text file from placeholder directory if specified
    if statement_run_bool:
        import shutil
        txt_files = [f for f in os.listdir(sys_txt_src_dir) if f.endswith(".txt")]
        if txt_files:
            src_txt = os.path.join(sys_txt_src_dir, txt_files[0])
            dst_txt = os.path.join(results_basepath, txt_files[0])
            if not os.path.exists(dst_txt):
                shutil.copy2(src_txt, dst_txt)
                _logger.info("Copied %s to %s", txt_files[0], results_basepath)
            else:
                _logger.info("System text file already exists in %s", results_basepath)

    pols_path = os.path.join(results_basepath, "pols_structure.json")
    if not os.path.exists(pols_path):
        raise FileNotFoundError(f"Could not find pols_structure.json at: {pols_path}")

    # unpacks the JSON data dump to dictionary
    with open(pols_path, "r") as f:
        pols = json.load(f)

    # Normalize: pols may have string keys "0","1",...
    if isinstance(pols, dict):
        pol_items = list(pols.items())
    else:
        raise ValueError("pols_structure.json is not a dict at the top level.")

    # ---------- Extract Policy-Level items for AS from program JSON data dump ----------
    from policy_structure.Policy import Policy

    pol_level_rows: List[Dict[str, Any]] = []
    for _, pol in pol_items:
        pol_num = pol.get("num") or pol.get("PC_CONT")
        as_items = Policy.extract_pol_level_items(pol)
        
        row = {"policy_num": pol_num}
        row.update({k: float(v) for k, v in as_items.items()})
        pol_level_rows.append(row)

    pol_items_df = pd.DataFrame(pol_level_rows)

    # ---------- Extract Fund-Level items for AS from program JSON data dump ----------
    fund_items_rows: List[Dict[str, Any]] = []

    for _, pol in pol_items:
        pol_num = pol.get("num") or pol.get("PC_CONT")
        fund_items = Policy.extract_fund_level_items(pol)

        for item in fund_items:
            fund_items_rows.append(dict(
                **item,
            ))

    fund_items_df = pd.DataFrame(fund_items_rows)

    # ---------- Extract Transaction-Fund-Level items for AS from program JSON data dump ----------
    txn_fund_items_rows: List[Dict[str, Any]] = []

    for _, pol in pol_items:
        txn_fund_items = Policy.extract_txn_fund_level_items(pol)

        for item in txn_fund_items:
            txn_fund_items_rows.append(item)

    txn_fund_items_df = pd.DataFrame(txn_fund_items_rows)


    # ---------- Parse AS system text file (policy-level, fund-level, txn-fund-level) ----------
    sys_txt_path = dst_txt
    if os.path.exists(sys_txt_path):
        # Filter text file to only lines belonging to policies in pols_structure.json
        policy_nums = {pol.get("num") or pol.get("PC_CONT") for _, pol in pol_items}
        filtered_lines = _filter_sys_txt_by_policies(sys_txt_path, policy_nums)
        _logger.info("Filtered system text file: %d lines for %d policies", len(filtered_lines), len(policy_nums))

        sys_pol_df = _parse_sys_pol_level(filtered_lines)
        sys_fund_df = _parse_sys_fund_level(filtered_lines)
        sys_txn_fund_df = _parse_sys_txn_fund_level(filtered_lines)
    else:
        _logger.warning("System text file not found: %s", sys_txt_path)
        sys_pol_df = pd.DataFrame()
        sys_fund_df = pd.DataFrame()
        sys_txn_fund_df = pd.DataFrame()

    # ---------- Write output ----------
    # ---------- Create combined comparison policy-level Excel ----------
    _export_pol_comparison_to_excel(pol_items_df, sys_pol_df, results_basepath)

    # ---------- Create combined comparison fund-level Excel ----------
    _export_fund_comparison_to_excel(fund_items_df, sys_fund_df, results_basepath)

    # ---------- Create combined comparison txn-fund-level Excel ----------
    _export_txn_fund_comparison_to_excel(txn_fund_items_df, sys_txn_fund_df, results_basepath)

    return pol_items_df, fund_items_df, txn_fund_items_df, sys_pol_df, sys_fund_df, sys_txn_fund_df

if __name__ == "__main__":
    run_compare_process(r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Output_RILA\2026-04-10\b3224560-19cb-44eb-be10-155a48bc77f5", 
                        r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Input_RILA\RILA_Validation_UI_v1_Step1.xlsx")
    pass
