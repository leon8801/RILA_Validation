# post_process.py
# ruff: noqa: F541
import os
import json
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime
import pandas as pd
# from logger import create_logger
# _logger = create_logger(__name__)
import logging
_logger = logging.getLogger(__name__)
# ----------------------------
# Helpers
# ----------------------------

def _safe_get(d: Dict[str, Any], *keys, default=None):
    """Safely walk nested dict using successive keys; return default if missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _txntype(row: Dict[str, Any]) -> str:
    """Normalize transaction type string for comparisons."""
    # prefer a friendlier name if available
    v = row.get("transaction_code")
    if v is not None:
        v = row.get("transaction_type")
    s = str(v) if v is not None else ""
    return s.strip()

def _txn_amount(row: Dict[str, Any]) -> float:
    """Try several possible fields for transaction amount; return 0.0 if none."""
    for k in ("txn_amount", "TXN_AMT", "TC_AMT", "amount"):
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    return 0.0

def _date_from(s: Any) -> Optional[pd.Timestamp]:
    if s is None:
        return None
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return None

def _len_dictlike(d: Any) -> int:
    if isinstance(d, dict):
        return len(d)
    return 0

def _last_index_key(d: Dict[str, Any]) -> Optional[Any]:
    """Get last key in an index-like dict (keys may be strings or ints)."""
    if not isinstance(d, dict) or not d:
        return None
    # preserve insertion order (should be from OrderedDict originally)
    return list(d.keys())[-1]

def _fund_value(fund: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    """Try multiple field names to fetch a float; return None if missing/unparseable."""
    for k in keys:
        if k in fund and fund[k] is not None:
            try:
                return round(float(fund[k]), 2)
            except Exception:
                pass
    return None


def _sys_value(txn: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    """Fetch float from transaction dict, rounded to 2 decimals. Defaults to 0.0 if missing/unparseable."""
    for k in keys:
        if k in txn and txn[k] is not None:
            try:
                return round(float(txn[k]), 2)
            except Exception:
                pass
    return None

def _count_txn_types(policy: Dict[str, Any]) -> Dict[str, int]:
    """Count txn types by categories."""
    counts = {
        "total_transactions": 0,
        "deposit": 0,
        "bonus": 0,
        "fee": 0,
        "withdrawal": 0,
        "reallocation": 0,
        "anniversary": 0
    }
    days = policy.get("days", {}) or {}
    for _, day in days.items():
        txns = day.get("transactions", {}) or {}
        for _, txn in txns.items():
            counts["total_transactions"] += 1
            # t = _txntype(txn).lower()
            t = _txntype(txn)
            if t == "Initial Premium":
                counts["deposit"] += 1
            elif t == "Premium Bonus":
                counts["bonus"] += 1
            elif t == "ROP Death Benefit Fee":  # guard for minor variants
                counts["fee"] += 1
            elif t == "Withdrawal":
                counts["withdrawal"] += 1
            elif t == "Reallocation Transfer":
                counts["reallocation"] += 1
            elif t == "Index Valuation":
                counts["anniversary"] += 1
    return counts

def _sum_amounts_by_type(policy: Dict[str, Any]) -> Dict[str, float]:
    """Sum txn_amount by categories."""
    sums = {
        "premium": 0.0,     # Deposit + Premium Bonus
        "tot_wd": 0.0,      # Withdrawal
        "tot_fee": 0.0,     # GLB/GMWB Fee
    }
    days = policy.get("days", {}) or {}
    for _, day in days.items():
        txns = day.get("transactions", {}) or {}
        for _, txn in txns.items():
            amt = _txn_amount(txn)
            t = _txntype(txn).lower()
            if t in ("initial premium"):
                sums["premium"] += amt
            elif t == "withdrawal":
                sums["tot_wd"] += amt
            elif t in ("rop death benefit fee"):
                sums["tot_fee"] += amt
    return sums

def _bop_eop_funds_counts(policy: Dict[str, Any]) -> Tuple[int, int]:
    """Count funds in first transaction of first day and last transaction of last day."""
    days = policy.get("days", {}) or {}
    if not days:
        return 0, 0
    # first day/txn
    first_day_key = list(days.keys())[0]
    first_day = days[first_day_key]
    first_txns = first_day.get("transactions", {}) or {}
    if first_txns:
        first_txn_key = list(first_txns.keys())[0]
        first_txn = first_txns[first_txn_key]
        bop_ct = _len_dictlike(first_txn.get("funds", {}))
    else:
        bop_ct = 0
    # last day/txn
    last_day_key = list(days.keys())[-1]
    last_day = days[last_day_key]
    last_txns = last_day.get("transactions", {}) or {}
    if last_txns:
        last_txn_key = list(last_txns.keys())[-1]
        last_txn = last_txns[last_txn_key]
        eop_ct = _len_dictlike(last_txn.get("funds", {}))
    else:
        eop_ct = 0
    return bop_ct, eop_ct

def _policy_last_txn(policy: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (last_txn_dict, last_day_key) for a policy."""
    days = policy.get("days", {}) or {}
    if not days:
        return None, None
    last_day_key = list(days.keys())[-1]
    last_day = days[last_day_key]
    txns = last_day.get("transactions", {}) or {}
    if not txns:
        return None, last_day_key
    last_txn_key = list(txns.keys())[-1]
    return txns[last_txn_key], last_day_key

def _policy_first_txn(policy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    days = policy.get("days", {}) or {}
    if not days:
        return None
    first_day_key = list(days.keys())[0]
    first_day = days[first_day_key]
    txns = first_day.get("transactions", {}) or {}
    if not txns:
        return None
    first_txn_key = list(txns.keys())[0]
    return txns[first_txn_key]

# ---------- NEW (safe): Build policy_level_calc_value from data_with_rollforward_policy_level.csv ----------
def _load_policy_level_calc_value(results_basepath: str) -> pd.DataFrame:
    """
    Best-effort reader:
      - If file is missing or unreadable, return empty DataFrame.
      - If present, pick last row per PC_CONT (max txn num -> latest date),
        and select the requested columns if available.
    Never raises.
    """
    try:
        plc_csv_path = os.path.join(results_basepath, "4. Data_Pol_Level_wRollforward.csv")
        if not os.path.exists(plc_csv_path):
            return pd.DataFrame()

        plc = pd.read_csv(plc_csv_path)

        # Robust sort helpers
        #plc["__txn_num_float"] = pd.to_numeric(plc.get("TRANSACTION_NUM"), errors="coerce")
        plc["__date"] = pd.to_datetime(plc.get("EFFECTIVE_DATE"), errors="coerce")
        plc["__row_id"] = range(len(plc))  # stable tiebreaker

        plc_sorted = plc.sort_values(
            by=["POLICY_NUM", "__date", "TT_TXN_TYPE_CODE", "__row_id"],
            ascending=[True, True, True, True],
        )

        # Last txn per policy (tail(1) per group after sort)
        last_rows = plc_sorted.groupby("POLICY_NUM", as_index=False, sort=False).tail(1)

        wanted = [
            "POLICY_NUM", "TRANSACTION_NUM","TRANSACTION_DESC", "EFFECTIVE_DATE",
            "av_end_txn", "ACCOUNT_VAL", "mwv_interim", "SYS_MWV_Pol",
            "csv", "SYS_CSV", "death_ben", "TC_DB_PROCEEDS"
        ]
        cols = [c for c in wanted if c in last_rows.columns]
        out = last_rows[cols].copy()

        out.columns = ['Policy Number', 'Transaction Number','Transaction Type', 'Transaction Date',
                       'AV', 'SYS_AV', 'MWV', 'SYS_MWV', 'CSV', 'SYS_CSV',
                       'DB', 'SYS_DB']
        out['diff_av'] = out['SYS_AV'].sub(out['AV'], fill_value=0)
        out['diff_mwv'] = out['SYS_MWV'].sub(out['MWV'], fill_value=0)
        out['diff_csv'] = out['SYS_CSV'].sub(out['CSV'], fill_value=0)
        out['diff_db'] = out['SYS_DB'].sub(out['DB'], fill_value=0)
        # Save snapshot (optional)
        if not out.empty:
            out.to_csv(os.path.join(results_basepath, "policy_level_calc_value.csv"), index=False)

        return out
    except Exception:
        # Silently degrade: return empty so the main flow never breaks
        return pd.DataFrame()

# ----------------------------
# Main entry point
# ----------------------------

def run_post_process(results_basepath: str) -> Tuple[int, pd.DataFrame, pd.DataFrame]:
    """
    Read pols_structure.json in results_basepath and produce:
      - general_summary table
      - policy_summary table (one row per policy)
      - fund_summary tables (one per policy)
    All written to: results_basepath/post_process_summary.txt
    """
    os.makedirs(results_basepath, exist_ok=True)
    pols_path = os.path.join(results_basepath, "pols_structure.json")
    if not os.path.exists(pols_path):
        raise FileNotFoundError(f"Could not find pols_structure.json at: {pols_path}")

    with open(pols_path, "r") as f:
        pols = json.load(f)

    # Normalize: pols may have string keys "0","1",...
    if isinstance(pols, dict):
        # keep insertion order
        pol_items = list(pols.items())
    else:
        raise ValueError("pols_structure.json is not a dict at the top level.")

    # ---------- General summary ----------
    total_num_of_policy = len(pols)

    def _policy_flag(d: Dict[str, Any], name: str) -> bool:
        # try policy-level flags; if missing, try first/last txn flags; else False
        val = d.get(name)
        if isinstance(val, bool):
            return val
        # fallback from first txn
        ft = _policy_first_txn(d)
        if ft:
            v2 = ft.get(name)
            if isinstance(v2, bool):
                return v2
        # fallback from last txn
        lt, _ = _policy_last_txn(d)
        if lt:
            v3 = lt.get(name)
            if isinstance(v3, bool):
                return v3
        return False

    num_of_gmdb_pol = total_num_of_policy
    _logger.info(f"creating summary...")
    general_summary = pd.DataFrame(
        [{
            "total_num_of_policy": total_num_of_policy,
            "num_of_gmdb_pol": num_of_gmdb_pol
        }]
    )

    # ---------- Policy summary ----------
    policy_rows: List[Dict[str, Any]] = []
    # For policy difference list (new)
    diff_rows: List[Dict[str, Any]] = []

    for idx, pol in pol_items:
        # index label
        try:
            idx_int = int(idx)
        except Exception:
            idx_int = idx

        pc = pol.get("num") or pol.get("PC_CONT")
        # is_gmwb = _policy_flag(pol, "is_gmwb")
        # is_multiplier = _policy_flag(pol, "is_multiplier")

        counts = _count_txn_types(pol)
        sums = _sum_amounts_by_type(pol)
        other_ct = (
            counts["total_transactions"]
            - counts["deposit"]
            # - counts["bonus"]
            - counts["fee"]
            - counts["withdrawal"]
            - counts["reallocation"]
            - counts["anniversary"]
        )
        bop_ct, eop_ct = _bop_eop_funds_counts(pol)

        policy_rows.append(
            dict(
                idx=idx_int,
                PC_NUM=pc,
                # is_gmwb=is_gmwb,
                # is_multiplier=is_multiplier,
                # PO_ISS_DATE=iss_date,
                transactions=counts["total_transactions"],
                deposit=counts["deposit"],
                # bonus=counts["bonus"],
                fee=counts["fee"],
                withdrawal=counts["withdrawal"],
                realocation=counts["reallocation"],
                anniversary=counts["anniversary"],
                other_trxn=other_ct,
                bop_funds=bop_ct,
                eop_funds=eop_ct,
                tot_premium_amt=round(sums["premium"], 2),
                tot_wd_amt=round(sums["tot_wd"], 2),
                tot_fee_amt=round(sums["tot_fee"], 2),
            )
        )
    policy_summary = pd.DataFrame(policy_rows).sort_values(by=["idx"]).reset_index(drop=True)

    # ---------- Fund summary (per policy) ----------
    def build_fund_summary_for_policy(pol: Dict[str, Any]) -> pd.DataFrame:
        # last txn context
        last_txn, _ = _policy_last_txn(pol)
        pol_num = pol.get("num") or pol.get("PC_CONT")
        issDate = pol.get("issDate")
        pol_yr = last_txn.get("pol_yr")

        last_txn_date = last_txn.get("date") if last_txn else None
        last_txn_date = _date_from(last_txn_date)
        last_txn_date_str = last_txn_date.date().isoformat() if isinstance(last_txn_date, pd.Timestamp) else last_txn_date

        funds = (last_txn or {}).get("funds", {}) or {}

        rows = []
        total_diff_av = total_diff_mwv = 0.0

        # difference row (sum - system)
        def _diff(a, b):
            if a is None or b is None:
                return None
            try:
                return float(a) - float(b)
            except Exception:
                return None

        for _, fund in funds.items():
            code = fund.get("raw_fund_code") or fund.get("fund_code") or fund.get("code")
            av = _fund_value(fund, ("av", "av_eop", "account_value"))
            mwv = _fund_value(fund, ("mwv",))
            sys_av = _fund_value(fund, ("sys_av",))
            sys_mwv = _fund_value(fund, ("sys_mwv",))
            diff_av = _diff(av, sys_av)
            diff_mwv = _diff(mwv, sys_mwv)
            total_diff_av += diff_av or 0.0
            total_diff_mwv += diff_mwv or 0.0

            rows.append(dict(
                fund_code=code,
                av = av, mwv = mwv, 
                sys_av = sys_av, sys_mwv = sys_mwv,
                diff_av = diff_av, diff_mwv = diff_mwv
            ))


        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
            "fund_code", "av", "mwv", 
            "sys_av", "sys_mwv", 
            "diff_av", "diff_mwv"
        ])

        # total diff row
        total_diff_row = dict(
            fund_code="Total Diff",
            diff_av=total_diff_av, diff_mwv=total_diff_mwv
        )
        df = pd.concat([df, pd.DataFrame([total_diff_row])], ignore_index=True)

        total_diff_row_filled = {k: (v if v is not None else 0.0) for k, v in total_diff_row.items()}

        # attach header-ish info as attributes (for printing / meta data)
        df.attrs["pol_num"] = pol_num
        df.attrs["iss_date"] = issDate
        df.attrs["last_transaction_date"] = last_txn_date_str
        df.attrs["policy_year"] = pol_yr

        # --- also collect a policy-level diff summary row (for email list) ---
        # is_gmwb_flag = _policy_flag(pol, "is_gmwb")
        # is_multiplier_flag = _policy_flag(pol, "is_multiplier")

        diff_rows.append(dict(
            PC_CONT=pol_num,
            diff_av=total_diff_row_filled.get("diff_av", 0.0),
            diff_mwv=total_diff_row_filled.get("diff_mwv", 0.0)
        ))

        return df

    fund_summaries: List[Tuple[Any, pd.DataFrame]] = []
    for idx, pol in pol_items:
        fund_df = build_fund_summary_for_policy(pol)
        fund_summaries.append((idx, fund_df))

    # ---------- Build fund_difference_list (new) ----------
    fund_difference_list = pd.DataFrame(diff_rows)[[
        "PC_CONT",  "diff_av", "diff_mwv"
    ]].copy()

    # threshold check: fail if |av_diff| OR |income_base_diff| > threshold
    diff_threshold = 10.0
    fund_difference_list["is_pass_threshold"] = ~(
            (fund_difference_list["diff_av"].abs() > diff_threshold) | 
            (fund_difference_list["diff_mwv"].abs() > diff_threshold)
    )

    total_policy_not_pass = int((~fund_difference_list["is_pass_threshold"]).sum())

    # Save reduced (only not-passed) lists
    failing = fund_difference_list[~fund_difference_list["is_pass_threshold"]].copy()
    failing_txt = os.path.join(results_basepath, "fund_difference_list.txt")
    failing_csv = os.path.join(results_basepath, "fund_difference_list.csv")
    if not failing.empty:
        with open(failing_txt, "w", encoding="utf-8") as f:
            f.write(f"Policies not passing threshold (> {diff_threshold}):\n\n")
            f.write(failing.to_string(index=False))
            failing.to_csv(failing_csv, index=False)

    # ---------- Build Pol_difference_list (new) ----------
    policy_level_calc_value = _load_policy_level_calc_value(results_basepath)
    policy_difference_list = pd.DataFrame(policy_level_calc_value)[[
        "Policy Number",  "diff_av", "diff_mwv", "diff_csv", "diff_db"
    ]].copy()
    # threshold check: fail if av, mwv, csv, db > threshold
    policy_difference_list["is_pass_threshold"] = ~(
            (policy_difference_list["diff_av"].abs() > diff_threshold) | 
            (policy_difference_list["diff_mwv"].abs() > diff_threshold) | 
            (policy_difference_list["diff_csv"].abs() > diff_threshold) |
            (policy_difference_list["diff_db"].abs() > diff_threshold)
    )
    # Save reduced (only not-passed) lists
    failing = policy_difference_list[~policy_difference_list["is_pass_threshold"]].copy()
    failing_txt = os.path.join(results_basepath, "policy_difference_list.txt")
    failing_csv = os.path.join(results_basepath, "policy_difference_list.csv")
    if not failing.empty:
        with open(failing_txt, "w", encoding="utf-8") as f:
            f.write(f"Policies not passing threshold (> {diff_threshold}):\n\n")
            f.write(failing.to_string(index=False))
            failing.to_csv(failing_csv, index=False)

    # ---------- Write everything to a single .txt ----------
    out_txt = os.path.join(results_basepath, "post_process_summary.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"Post-Process Summary\nGenerated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("-" * 80 + "\n\n")

        f.write("\n\n=========================GENERAL SUMMARY=========================\n\n")
        f.write(general_summary.to_string(index=False))
        # f.write("\n\n" + "-" * 80 + "\n\n")

        f.write("\n\n=========================POLICY SUMMARY=========================\n\n")
        if not policy_summary.empty:
            f.write(policy_summary.to_string(index=False))
        else:
            f.write("(no policies)\n")
        # f.write("\n\n" + "-" * 80 + "\n\n")

        f.write("\n\n=========================FUND SUMMARY (per policy)=========================\n\n")
        for idx, df in fund_summaries:
            pol_num = df.attrs.get("pol_num")
            iss_date = df.attrs.get("iss_date")
            last_date = df.attrs.get("last_transaction_date")
            pol_yr = df.attrs.get("policy_year")

            f.write(f"policy_idx: {idx}\n")
            f.write(f"pol_num: {pol_num}\n")
            f.write(f"policy_year: {pol_yr}\n")
            f.write(f"issue_date: {iss_date}\n")
            f.write(f"latest_transaction_date: {last_date}\n\n")


            if not df.empty:
                f.write(df.to_string(index=False, na_rep=''))
            else:
                f.write("(no funds)\n")
            f.write("\n" + "-" * 80 + "\n\n")

        f.write("\n\n=========================POLICY-LEVEL CALC VALUES (last txn per policy)=========================\n\n")
        if not policy_level_calc_value.empty:
            f.write(policy_level_calc_value.to_string(float_format= lambda x: "{:.2f}".format(x), index=False))
        else:
            f.write("(policy-level CSV not found or no rows)\n")

    # Also export the two summary tables as CSV (optional but handy)
    # general_summary.to_csv(os.path.join(results_basepath, "general_summary.csv"), index=False)
    # policy_summary.to_csv(os.path.join(results_basepath, "policy_summary.csv"), index=False)
    # and each fund summary as its own CSV
    for idx, df in fund_summaries:
        pol_num = df.attrs.get("pol_num") or f"policy_{idx}"
        safe_name = str(pol_num).replace("/", "-").replace("\\", "-").replace(" ", "_")
        df.to_csv(os.path.join(results_basepath, f"fund_level_calc_value_{safe_name}.csv"), index=False)

    # Return for email step
    return total_policy_not_pass, policy_difference_list, general_summary

if __name__ == "__main__":
    # Example:
    run_post_process(r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Output_RILA\2026-03-28\bd52ca42-37f7-4e9e-9fe3-f6cc30c43a97")
    pass