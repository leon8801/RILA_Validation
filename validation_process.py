# ruff: noqa: F541, F401
from __future__ import annotations

import os
import json
import pandas as pd
import numpy as np
import datetime
import logging
import shutil
from RollForward_base import RollForward_base
from RollForward_policy import RollForward_policy
from Data_loader import retrieve_data_from_Navisys
from dateutil import parser
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay
from typing import Iterable
from typing import Optional

_logger = logging.getLogger(__name__)
us_bus = CustomBusinessDay(calendar=USFederalHolidayCalendar())


class _PolsDictEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles Decimal, Timestamp, and numpy types."""
    def default(self, o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if hasattr(o, 'isoformat'):  # datetime, date, pd.Timestamp
            return o.isoformat()
        # Decimal has no isoformat in older Python, use str
        if hasattr(o, '__float__') and hasattr(o, '__str__'):
            # Catch Decimal without being too broad
            try:
                float_val = float(o)
                str_val = str(o)
                if str_val.replace('.', '', 1).replace('-', '', 1).isdigit():
                    return float_val
            except (ValueError, TypeError):
                pass
        return super().default(o)




def sort_gmdb_with_rollforward(df: pd.DataFrame) -> pd.DataFrame:
    """
    Sort by PC_CONT (policy), then TRANSACTION_NUM (numeric order), then raw_fund_code (alpha).
    Preserves the original TRANSACTION_NUM column as-is (string), using a temporary numeric key.
    """
    required = ["POLICY_NUM", "TRANSACTION_NUM", "raw_fund_code"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"Missing required columns: {missing}")

    out = df.copy()

    # Build a numeric sort key for TRANSACTION_NUM without altering the original column
    # Coerce to numeric; NaNs (if any) sort last
    out["_txn_num_sort"] = pd.to_numeric(out["TRANSACTION_NUM"], errors="coerce")

    # Stable sort to preserve prior row order within equal keys
    out = out.sort_values(
        by=["PC_CONT", "_txn_num_sort", "raw_fund_code"],
        ascending=[True, True, True],
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)

    # Drop temp key
    out = out.drop(columns=["_txn_num_sort"])

    return out

def attach_rollforward_columns(
    base_df: pd.DataFrame,
    rollforward_results: pd.DataFrame,
    keep_columns: Optional[Iterable[str]] = None,
    join_keys: Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    Attach selected columns from rollforward_results to base_df.

    Args:
        base_df:            Your base dataframe (e.g., old extract_data or gmdb_long).
        rollforward_results:Dataframe produced by rollforward step.
        keep_columns:       Which columns from rollforward_results to attach.
                            - If None, keep ALL columns from rollforward_results.
                            - If provided, we keep the intersection of these with actual columns.
        join_keys:          If provided, perform a LEFT MERGE on these keys.
                            If None, do an index-aligned horizontal CONCAT (after dropping dup cols).

    Returns:
        A dataframe with rollforward columns attached safely (no duplicate columns).
    """
    # 1) Decide which rollforward columns to keep
    if keep_columns is None:
        cols_to_keep = list(rollforward_results.columns)
    else:
        cols_to_keep = [c for c in keep_columns if c in rollforward_results.columns]

    # 2) If merging on keys
    if join_keys:
        # Ensure keys are present on both sides
        missing_in_base = [k for k in join_keys if k not in base_df.columns]
        missing_in_roll = [k for k in join_keys if k not in rollforward_results.columns]
        if missing_in_base or missing_in_roll:
            raise KeyError(
                f"Join keys missing. In base missing: {missing_in_base}; "
                f"in rollforward missing: {missing_in_roll}"
            )

        # Keep keys + desired rollforward cols (and drop duplicates on keys if any)
        roll_cols = list(dict.fromkeys(list(join_keys) + cols_to_keep))  # preserve order, dedupe
        slim_roll = rollforward_results[roll_cols].drop_duplicates(subset=join_keys)

        # Drop overlapping columns from base (except join keys)
        overlapping = [c for c in cols_to_keep if c in base_df.columns and c not in join_keys]
        base_slim = base_df.drop(columns=overlapping) if overlapping else base_df

        # Merge
        merged = base_slim.merge(slim_roll, on=join_keys, how="left")
        return merged

    # 3) Otherwise, do index-aligned concat (classic behavior)
    #    - Drop any rollforward columns that already exist on base_df
    dup_cols = [c for c in cols_to_keep if c in base_df.columns]
    slim_roll = rollforward_results[[c for c in cols_to_keep if c not in dup_cols]].copy()

    # Align indices and concat horizontally
    combined = pd.concat(
        [base_df.reset_index(drop=True), slim_roll.reset_index(drop=True)],
        axis=1
    )
    return combined

def _build_measure_sets(fund_map: dict[str, tuple[Optional[str], Optional[str]]], df_cols) -> tuple[set, set, set]:
    """Return (prnl_cols, txn_cols, measure_cols) intersected with df columns."""
    prnl_cols = {c for (c, _) in fund_map.values() if c is not None and c in df_cols}
    txn_cols  = {c for (_, c) in fund_map.values() if c is not None and c in df_cols}
    return prnl_cols, txn_cols, prnl_cols.union(txn_cols)

# -----------------------------------------------------------
# 1) Find which fund codes each policy actually has
# -----------------------------------------------------------
def find_funds(df: pd.DataFrame,
               fund_map: dict[str, tuple[Optional[str], Optional[str]]],
               policy_col: str = "POLICY_NUM") -> dict[str, list[str]]:
    """
    For each policy (POLICY_NUM), decide which raw_fund_code(s) it has.
    Rule (per your spec): aggregate by policy over *TXN* columns in the mapping.
      - If the sum for that TXN column is non-zero (and not NA), policy has that fund.
      - If TXN column is missing for a fund (None), we *cannot* detect by this rule → excluded.
    Returns: dict { policy_value -> [raw_fund_code, ...] }
    """
    _, txn_cols, _ = _build_measure_sets(fund_map, df.columns)

    # If no txn columns exist in the file, every policy gets an empty list
    if not txn_cols:
        return {str(k): [] for k in df[policy_col].astype(str).unique()}

    # Sum TXN columns by policy
    grouped = df.groupby(policy_col, dropna=False)[list(txn_cols)].sum(numeric_only=True)

    # For each policy, pick codes whose mapped TXN column sum != 0
    policies = {}
    for pol, row in grouped.iterrows():
        codes = []
        for code, (_prnl, txn) in fund_map.items():
            if txn is None or txn not in row.index:
                continue  # can’t detect this fund via TXN per your rule
            val = row.get(txn)
            if pd.notna(val) and float(val) != 0.0:
                codes.append(code)
        policies[str(pol)] = codes
    return policies

# -----------------------------------------------------------
# 2/3/4) Unpivot by policy using reduced map per policy
# -----------------------------------------------------------
def unpivot_gmdb_by_policy(df: pd.DataFrame,
                           fund_map: dict[str, tuple[Optional[str], Optional[str]]],
                           fund_code_map: dict[str, tuple[Optional[str], Optional[str]]],
                           policy_col: str = "POLICY_NUM") -> pd.DataFrame:
    """
    Build a longer table with fewer duplicates by:
      - Finding each policy’s actual fund codes (using TXN sums)
      - Creating a reduced map per policy
      - Looping per policy *then* per code to create rows only for funds that exist
    Output columns: (all id columns) + raw_fund_code + PRNL_amt + TXN_AMT
    """
    _, _, measure_cols = _build_measure_sets(fund_map, df.columns)
    id_cols = [c for c in df.columns if c not in measure_cols]

    # figure funds by policy
    funds_per_policy = find_funds(df, fund_map, policy_col=policy_col)

    out_frames = []
    # group rows by policy once (so we don’t keep re-filtering)
    for pol_val, policy_df in df.groupby(policy_col, dropna=False):
        pol_key = str(pol_val)
        codes_for_policy = funds_per_policy.get(pol_key, [])

        if not codes_for_policy:
            # No mapped funds detected for this policy (via TXN sums) → skip
            # (alternatively, emit rows for codes with PRNL only; your rule said TXN-based)
            continue

        # For this policy, build rows only for the needed codes
        for code in codes_for_policy:
            prnl_col, txn_col = fund_map.get(code, (None, None))
            mwv_col = "STRATEGY_MWV_" + code.upper()
            _, fund_name = fund_code_map.get(code, (None, None))
            sub = policy_df[id_cols].copy()
            sub["raw_fund_code"] = code
            sub['fund_name'] = fund_name

            # PRNL_amt: 0 if no PRNL column for this code
            if prnl_col is None or prnl_col not in policy_df.columns:
                sub["PRNL_amt"] = 0
            else:
                sub["PRNL_amt"] = policy_df[prnl_col].fillna(0)

            # TXN_AMT: 0 if no TXN column for this code (should exist by detection, but be safe)
            if txn_col is None or txn_col not in policy_df.columns:
                sub["TXN_AMT"] = 0
            else:
                sub["TXN_AMT"] = policy_df[txn_col].fillna(0)

            # SYS_MWV: 0 if no MWV column for this code
            if mwv_col is None or mwv_col not in policy_df.columns:
                sub["SYS_MWV"] = 0
            else:
                sub["SYS_MWV"] = policy_df[mwv_col].fillna(0)

            out_frames.append(sub)

    if not out_frames:
        # No policies had any mapped funds by the TXN rule
        return pd.DataFrame(columns=id_cols + ["raw_fund_code", "PRNL_amt", "TXN_AMT"])

    gmdb_long = pd.concat(out_frames, ignore_index=True)
    return gmdb_long


def read_excel_user_inputs(UI_file_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        df_run_options = pd.read_excel(UI_file_path, sheet_name='Run_Options')
        df_queries = pd.read_excel(UI_file_path, sheet_name='Queries')
    except PermissionError:
        raise PermissionError(
            f"Cannot read '{UI_file_path}'. "
            "The file may be open in Excel with exclusive access. "
            "Please save and close it, then try again."
        )
    return df_run_options, df_queries

def read_excel_fund_mapping(UI_file_path: str) -> dict[str, dict]:
    try:
        df = pd.read_excel(UI_file_path, sheet_name='Fund_Mapping')
    except PermissionError:
        raise PermissionError(
            f"Cannot read '{UI_file_path}'. "
            "The file may be open in Excel with exclusive access. "
            "Please save and close it, then try again."
        )

    # Txn Fields dict
    key_field = 'Fund_Code_1'
    value_fields = ['Fund_Value_Field', 'Fund_Txn_Amt_Field']
    FUND_MAP_NEW = df.set_index(key_field)[value_fields].apply(tuple, axis=1).to_dict()

    # Index rate fields Dict[str, Dict[str, str]]
    key_field = 'Fund_Code_1'
    value_fields = ['buffer', 'cap', 'par', 'trig', 'lock', 'lock_id']
    FUND_PARAMETER_MAP_NEW = df.set_index(key_field)[value_fields].to_dict(orient='index')

    # Equity Index name Dict[str, str]
    FUND_INDEX_MAP_NEW = df.set_index('Fund_Code_1')['Equity_Index'].to_dict()

    # Alternative fund code and fund full name Dict[str, tuple(2)]
    key_field = 'Fund_Code_1'
    value_fields = ['Fund_Code_2', 'Fund_Name']
    FUND_CODE_MAP_NEW = df.set_index(key_field)[value_fields].apply(tuple, axis=1).to_dict()

    dict_group = {
        "FUND_TXN_MAP": FUND_MAP_NEW,
        "FUND_INDEX_MAP": FUND_PARAMETER_MAP_NEW,
        "FUND_EQINDEX_MAP": FUND_INDEX_MAP_NEW,
        "FUND_CODE_MAP": FUND_CODE_MAP_NEW
    }

    return dict_group


def build_data_long_from_sources(
    extract_data_db: pd.DataFrame,
    results_basepath: str,
    FUND_TXN_MAP: dict,
    FUND_CODE_MAP: dict
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    1) Unpivot to fund-level → data_long
    """
    os.makedirs(results_basepath, exist_ok=True)

    # 1) Unpivot to long fund rows
    data_long = unpivot_gmdb_by_policy(
        df=extract_data_db,
        fund_map=FUND_TXN_MAP,
        fund_code_map = FUND_CODE_MAP,
        policy_col="POLICY_NUM"
    )
    data_long.to_csv(os.path.join(results_basepath, "1. Data_Txn_Level_Original.csv"), index=False)
    return data_long, extract_data_db

def run_validation_process(UI_file_path: str,
                           surr_file_path: str,
                           results_basepath: str, 
                           input_base: str) -> tuple[pd.DataFrame, dict]:
    os.makedirs(results_basepath, exist_ok=True)

    # get run option for cycle date and/or pol list
    
    df_run_options, df_queries = read_excel_user_inputs(UI_file_path)
    cycle_date = df_run_options["Cycle Date"].iloc[0]
    pol_list = df_run_options["Policy List"]
    file_list = df_queries.set_index('Items')['SQL Output File Name'].to_dict()

    # if no cycle date, use business day minus 1
    if pd.isna(cycle_date):
        today = pd.Timestamp.now().normalize()
        cycle_date = (today - us_bus).strftime('%Y-%m-%d')
    # if there is cycle date specified, use cycle date, uniform format
    else:
        cycle_date = parser.parse(cycle_date).strftime('%Y-%m-%d')
    input_base = os.path.join(input_base, cycle_date)
    retrieve_data_from_Navisys(cycle_date, input_base, df_queries)

    file_list = {k: os.path.join(input_base, v) for k, v in file_list.items()}

    # read and save fund mapping info in memory for quick access
    fund_mapping = read_excel_fund_mapping(UI_file_path)

    # read main txn file
    extract_data_db = pd.read_csv(file_list["Main transaction data_Final"])

    # [Optional] Filter down by user sepcifield policy number(s)
    if pd.notnull(pol_list.iloc[0]):
        policies = pol_list.tolist()
        extract_data_db = extract_data_db[extract_data_db['POLICY_NUM'].isin(policies)]

    # unpivot main txn file for multiple funds
    _logger.info("Processing data...")
    data_long, data_full = build_data_long_from_sources(extract_data_db, results_basepath, 
                                                        fund_mapping.get("FUND_TXN_MAP"), fund_mapping.get("FUND_CODE_MAP") )

    # single-pass rollforward + nested dict
    _logger.info("Fund-level rollforward calculation starts...")
    rollforward_results, pols_dict = RollForward_base.get_rollforward_from_extract(
        data_by_fund=data_long,
        results_basepath=results_basepath,
        file_list = file_list,
        surr_file_path=surr_file_path,
        data_by_txn=data_full,
        fund_mapping = fund_mapping
    )
    _logger.info("Fund-level rollforward calculation finished")

    # run the policy-level rollforward (optional):
    _logger.info("Policy-level rollforward calculation starts")
    data_full_with_av = pd.read_csv(os.path.join(results_basepath, "3. Data_Pol_Level_wAV.csv"))
    pols_dict = RollForward_policy.get_rollforward_from_extract(
        data_long=data_full_with_av,
        results_basepath=results_basepath,
        surr_file_path=surr_file_path,
        fund_mapping = fund_mapping,
        pols_dict = pols_dict
    )
    _logger.info("Policy-level rollforward calculation finished")
    # Save nested dict JSON
    with open(os.path.join(results_basepath, "pols_structure.json"), "w") as f:
        json.dump(pols_dict, f, indent=2, cls=_PolsDictEncoder)

    return rollforward_results, pols_dict