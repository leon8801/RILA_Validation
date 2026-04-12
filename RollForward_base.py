# ruff: noqa: F541
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, date, timedelta
import os
import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay
from collections import OrderedDict
from PolicyTrxnDataJson import PolicyTrxnDataJson
import json
# from validation_data import FUND_PARAMETER_MAP, FUND_INDEX_MAP, FUND_CODE_MAP
import logging
_logger = logging.getLogger(__name__)
# =========================
# Helpers
# =========================

WDL_TYPES = {"Systematic Withdrawal", "Withdrawal", "Automatic Minimum Distribution", "72(t)/72(q) Withdrawal"}
LOCK_FUNDS = {"RO", "RN", "RM", "RP", "RR", "RQ"}
INDEXVALUATION_TYPES = {"Index Valuation"}
RESET_TYPES = {"Initial Premium", "Reallocation Transfer"}
ANNUAL_REFRESH_TYPES = {"Initial Premium", "Annual Processing"}
FIXED_FUND_CODES = {"RZ", "TB"}
cal = USFederalHolidayCalendar()
holidays = cal.holidays(start='2020-01-01', end = '2050-12-31')
custom_holidays = holidays.union([pd.Timestamp('2025-04-18')]) # adding in good friday in 2025
us_bus = CustomBusinessDay(calendar=custom_holidays)

def _to_str(val):
    return "" if val is None else str(val)

def _to_date(val: Any) -> Optional[date]:
    if val is None or (isinstance(val, float) and np.isnan(val)) or (isinstance(val, str) and val.strip() == ""):
        return None
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None

def _safe_float(x: Any, default: float = 0.0) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return default
    try:
        return float(x)
    except Exception:
        try:
            return float(str(x))
        except Exception:
            return default

def _months_between(d1: Optional[date], d2: Optional[date]) -> int:
    if not d1 or not d2:
        return 0
    # full months difference (approx; good enough for MVA N calc)
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


# Keep state per (policy, fund)
@dataclass
class GMDBState:
    # prepopulated
    pol_num: Optional[str] = None
    txn_type: Optional[str] = None
    txn_amt: float = 0.0
    fee: float = 0.0
    prem: float = 0.0
    txn_date: pd.Timestamp = pd.NaT
    txn_num: int = 0
    fund_code: Optional[str] = None
    fund_bal: float = 0.0
    freelook_rate: float = 0.0
    fixed_rate: float = 0.0
    alt_code: Optional[str] = None

    # trxn type
    prev_txn_type: Optional[str] = None
    # timing
    second_anniv: Optional[date] = None
    is_terminated: bool = False
    prev_is_2nd_anniv: bool = False
    is_irrelevant: bool = False

    # index tracking
    index_name: Optional[str] = None
    prev_index_value: Optional[float] = None  # previous observed index level used for return calc
    last_index_value: Optional[float] = None  # last index value carried forward
    last_index_return: Optional[float] = None # last computed index return (decimal)
    bop_index_value: Optional[float] = None # beginning of term index level
    prev_crediting_now: bool = False
    manual_locked: bool = False
    auto_locked: bool = False
    lock_credited: bool = False
    capture_level: float = 0.0
    auto_capture_funds: list[str] = field(default_factory=list)

    # strat base
    strat_base: Optional[float] = None  # strategy base

    # AV rollforward
    av_interim_before_wdl: float = 0.0 # for withdrawal calcs, saves the interim txn level av right before wdl
    av_end_txn: float = 0.0  # previous av_eop becomes next av_bop

    # cache parameter (if stable per fund)
    cap: Optional[float] = None
    participation: Optional[float] = None
    buffer: Optional[float] = None
    parameter: Optional[dict] = None

    # OUV/vix/trading cost Tracking
    bop_ouv: Optional[float] = None
    eop_ouv: Optional[float] = None
    vix: Optional[float] = None
    trading_cost: Optional[float] = None 

    # New trackers
    accum_wd: float = 0.0
    priorWd: float = 0.0
    is_withdrawal: bool = False
    is_freelook: bool = False
    accum_prem: float = 0.0
    pol_gross_wd: float = 0.0
    sys_pol_iv: float = 0.0
    pol_net_wd: float = 0.0
    fund_gross_wd: float = 0.0
    fund_net_wd: float = 0.0
    fwv: float = 0.0
    fund_portion: float = 0.0
    index_rate_ouv_lookup: dict = field(default_factory=dict)

    bop_date: Optional[date] = None
    bot_date: Optional[date] = None
    eot_date: Optional[date] = None
    eop_date: Optional[date] = None
    remaining_time_pct: Optional[float] = None
    pol_yr: int = 0
    deposit_ct: int = 0
    dt: float = 0.0

    mwv_rate: float = 0.0
    mwv: float = 0.0
    mwv_base: float = 0.0
    mva_index_bop: Optional[float] = None

    # boundary context for calculate_timing
    last_pol: Optional[str] = None
    last_day: Optional[date] = None
    last_txn: Optional[str] = None
    last_fund: Optional[str] = None



class RollForward_base:

    # -----------------------------
    # Main entry
    # -----------------------------
    @classmethod
    def get_rollforward_from_extract(cls,
                                     data_by_fund: pd.DataFrame,
                                     results_basepath: str,
                                     file_list: dict,
                                     surr_file_path: str,
                                     data_by_txn: pd.DataFrame,
                                     fund_mapping: dict) -> Tuple[pd.DataFrame, Dict[int, Dict[str, Any]]]:
        """
        Single-pass engine:
          - Prepare index (cached)
          - Sort data_long for stable state evolution
          - For each row: compute rollforward, attach sums by (policy, txn) using boundary logic,
            then ingest into nested dict
          - Returns (rollforward_results_df, pols_dict)
        Also saves rollforward CSV at results_basepath/rollforward.csv
        """
        import os
        os.makedirs(results_basepath, exist_ok=True)
        
        # unpack grouped dictionary from read excel sheet 
        # and make available for the entire class
        cls.FUND_TXN_MAP = fund_mapping["FUND_TXN_MAP"]
        cls.FUND_INDEX_MAP = fund_mapping["FUND_INDEX_MAP"]
        cls.FUND_EQINDEX_MAP = fund_mapping["FUND_EQINDEX_MAP"]
        cls.FUND_CODE_MAP = fund_mapping["FUND_CODE_MAP"]

        # --- Load csv and cache for fast lookups ---
        raw_index = pd.read_csv(file_list["Daily equity index quotes"])
        cls.index_df_cached = cls._prep_index_df(raw_index)

        ouv = pd.read_csv(file_list["Daily OUV values"])
        cls.ouv_df_cached = cls._prep_ouv_df(ouv)

        vix = pd.read_csv(file_list["Daily VIX quotes"])
        cls.vix_df_cached = cls._prep_vix_df(vix)

        auto_lock_funds = pd.read_csv(file_list["Auto Performance Capture Funds"])
        cls.auto_lock_funds_cached = cls._prep_alfunds_df(auto_lock_funds)

        # --- load surr_chrg_table and convert to dictionary for fast lookups ---
        surr_dict = cls.load_surr_chrg_table_to_dict(surr_file_path, results_basepath)
       
        sort_cols = [c for c in ["POLICY_NUM", "raw_fund_code"] if c in data_by_fund.columns]
        df = data_by_fund.sort_values(by=sort_cols, kind="mergesort").reset_index(drop=True)

        # --- Per-(policy, fund) GMDB state for rollforward ---
        states: Dict[Tuple[str, str], Any] = {}
        results: List[Dict[str, Any]] = []
        row_results: List[pd.Series] = []

        # --- Nested dict we build row-by-row (preserving your original structure) ---
        pols: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()

        # --- Boundary trackers (faithful to original naming) ---
        prev_pol: Optional[str] = None
        prev_day: Optional[date] = None
        prev_txn: Optional[Any] = None

        pol_idx: int = -1
        day_idx: int = -1
        txn_idx: int = -1

        # Keep a carry-forward aggregate when we don't update (unchanged between rows)
        last_aggregate: Dict[str, float] = {
            "total_fund_value": 0.0
        }
        _logger.info(f"processing each transaction row per policy and fund...")
        for _, row in df.iterrows():
            pol = _to_str(row.get("POLICY_NUM"))
            code = _to_str(row.get("raw_fund_code"))
            cur_day = _to_date(row.get("EFFECTIVE_DATE"))
            # Keep transaction number as *string* for stable grouping
            cur_txn = _to_str(row.get("TRANSACTION_NUM"))

            # ----- boundary logic (BEFORE computing current row) -----
            if pol != prev_pol:
                newPol = True
                newDay = True
                newTransaction = True
            elif cur_day != prev_day:
                newPol = False
                newDay = True
                newTransaction = True
            elif cur_txn != prev_txn:
                newPol = False
                newDay = False
                newTransaction = True
            else:
                newPol = newDay = newTransaction = False

            # ---- Aggregate totals for the *previous* completed transaction ----
            # - When newTransaction == True and newPol == False, we sum the just-completed (prev_pol, prev_txn)
            # - Otherwise, keep previous totals unchanged
            if newTransaction and not newPol and prev_pol is not None and prev_txn is not None:
                calc_aggregate = cls.aggregate(states=states, policy_num=prev_pol, txn_num=prev_txn)
                last_aggregate = calc_aggregate  # remember latest computed totals
            elif newPol:
                # Starting a new policy: reset totals
                last_aggregate = {
                    "total_fund_value": 0.0
                }
            else:
                # No boundary update: carry forward previous totals
                calc_aggregate = last_aggregate

            # ----- compute per-fund rollforward for the current row -----
            key = (pol, code)
            state = states.get(key)
            if state is None:
                state = GMDBState()
                states[key] = state

            # Debug by certain transaction step
            # if _ == 4:
            #     print("STOP")

            # NOTE: we compute per-fund AFTER aggregate (so aggregates are for the prior completed txn)
            calc_per_fund = cls.process_row(row, state, 
                                            cls.index_df_cached, cls.ouv_df_cached, cls.vix_df_cached,
                                            cls.auto_lock_funds_cached, surr_dict, pd.Series(last_aggregate))

            # combine the two parts for this row
            calc = {**last_aggregate, **calc_per_fund}

            # ----- attach to row & ingest -----
            row_w_rollforward = pd.concat([row, pd.Series(calc)], axis=0)

            # optional flags into results
            # calc_out = {**calc,
            #             "newPol": newPol_ingest,
            #             "newDay": newDay_ingest,
            #             "newTransaction": newTransaction_ingest}
            # results.append(calc_out)
            row_results.append(row_w_rollforward)

            # advance outer boundary trackers for next iteration
            prev_pol, prev_day, prev_txn = pol, cur_day, cur_txn

        # --- save data_with_rollforward.csv ---
        rollforward_results = pd.DataFrame(row_results)
        out_path = os.path.join(results_basepath, "2. Data_Txn_Level_wRollforward.csv")
        rollforward_results.to_csv(out_path, index=False)

        # Reorder fund level AVRF DF for loading dict/JSON structure
        # --- Stable sort (policy → txn → fund → date) for deterministic rollforward ---
        sort_cols = [c for c in ["POLICY_NUM", "EFFECTIVE_DATE", "TT_TXN_TYPE_CODE", "raw_fund_code"] if c in rollforward_results.columns]
        rollforward_results_resort = rollforward_results.sort_values(by=sort_cols, kind="mergesort").reset_index(drop=True)
        rollforward_results_resort.to_csv(os.path.join(results_basepath, "2. Data_Txn_Level_wRollforward_resort.csv"), index=False)
        # Boundaries reset
        prev_pol: Optional[str] = None
        prev_day: Optional[date] = None
        prev_txn: Optional[Any] = None
        pol_idx: int = -1
        day_idx: int = -1
        txn_idx: int = -1
        # re-loop through calc RF results in new order
        for _, row in rollforward_results_resort.iterrows():
            pol = _to_str(row.get("POLICY_NUM"))
            code = _to_str(row.get("raw_fund_code"))
            cur_day = _to_date(row.get("EFFECTIVE_DATE"))
            cur_txn = _to_str(row.get("TRANSACTION_NUM"))   # Keep transaction number as *string* for stable grouping
            if pol != prev_pol:
                newPol = True
                newDay = True
                newTransaction = True
            elif cur_day != prev_day:
                newPol = False
                newDay = True
                newTransaction = True
            elif cur_txn != prev_txn:
                newPol = False
                newDay = False
                newTransaction = True
            else:
                newPol = newDay = newTransaction = False
            (pols,
             pol_idx, day_idx, txn_idx, _, _, _) = PolicyTrxnDataJson.build_policies_structure(
                pols=pols,row=row,newPol=newPol,newDay=newDay,
                newTransaction=newTransaction,pol_idx=pol_idx,day_idx=day_idx,txn_idx=txn_idx,
            )
            prev_pol, prev_day, prev_txn = pol, cur_day, cur_txn


        # ---------- NEW: aggregate av_end_txn to policy/txn, join back to data_by_txn ----------
        if data_by_txn is not None:
            # select relevant fields, ensure consistent dtypes for join keys
            av_cols = [c for c in ["POLICY_NUM", "TRANSACTION_NUM", "av_end_txn"] if c in rollforward_results.columns]
            if set(["POLICY_NUM", "TRANSACTION_NUM", "av_end_txn"]).issubset(av_cols):
                av_policy_level = (
                    rollforward_results[["POLICY_NUM", "TRANSACTION_NUM", "av_end_txn"]]
                    .assign(TRANSACTION_NUM=lambda d: d["TRANSACTION_NUM"].astype("string"))
                    .groupby(["POLICY_NUM", "TRANSACTION_NUM"], as_index=False, sort=False)["av_end_txn"]
                    .sum()
                )

                mwv_policy_level = (
                    rollforward_results[["POLICY_NUM", "TRANSACTION_NUM", "mwv_interim"]]
                    .assign(TRANSACTION_NUM=lambda d: d["TRANSACTION_NUM"].astype("string"))
                    .groupby(["POLICY_NUM", "TRANSACTION_NUM"], as_index=False, sort=False)["mwv_interim"]
                    .sum()
                )

                df_full = data_by_txn.copy()
                if "TRANSACTION_NUM" in df_full.columns:
                    df_full["TRANSACTION_NUM"] = df_full["TRANSACTION_NUM"].astype("string")

                data_full_with_av = df_full.merge(
                    av_policy_level,
                    on=["POLICY_NUM", "TRANSACTION_NUM"],
                    how="left",
                    validate="m:1",
                    suffixes=("", "_from_funds")
                )

                data_full_with_av = data_full_with_av.merge(
                    mwv_policy_level,
                    on=["POLICY_NUM", "TRANSACTION_NUM"],
                    how="left",
                    validate="m:1",
                    suffixes=("", "_from_funds")
                )

                # save joined policy-level file with av_end_txn aggregated
                out_pol = os.path.join(results_basepath, "3. Data_Pol_Level_wAV.csv")
                data_full_with_av.to_csv(out_pol, index=False)

        # Convert OrderedDicts to plain dict for final output
        def _convert(o):
            from collections import OrderedDict as _OD
            if isinstance(o, _OD):
                return {k: _convert(v) for k, v in o.items()}
            if isinstance(o, dict):
                return {k: _convert(v) for k, v in o.items()}
            return o

        pols_dict = _convert(pols)
        return rollforward_results, pols_dict
    
    @staticmethod
    def prepopulate_state(row: pd.Series, state: GMDBState, FUND_CODE_MAP):
        """
        Populate the "easier" and more static items inside the state variable 
        to avoid repetitive referencing to [row]
        """
        # Generic items
        txn_type = str(row.get("TRANSACTION_DESC", "")).strip()
        state.txn_type = txn_type
        state.fee = _safe_float(row.get("GrWd+PErecap")) if txn_type == "ROP Death Benefit Fee" else 0.0
        state.prem = _safe_float(row.get("TXN_AMT")) if txn_type == "Initial Premium" else 0.0
        state.fund_bal = _safe_float(row.get("PRNL_amt"))
        state.txn_date = pd.to_datetime(row.get("EFFECTIVE_DATE")).date()
        state.txn_num = str(int(_safe_float(row.get("TRANSACTION_NUM"))))
        state.txn_amt = _safe_float(row.get("TXN_AMT"))
        state.fund_code = str(row.get("raw_fund_code") or "")
        state.pol_gross_wd = _safe_float(row.get("GrWd+PErecap"))
        state.sys_pol_iv = _safe_float(row.get("ACCOUNT_VAL"))
        state.pol_num = row.get("POLICY_NUM")
        state.freelook_rate = _safe_float(row.get("freelook rate")) if txn_type == "Initial Premium" else state.freelook_rate
        state.fixed_rate =  _safe_float(row.get("fixed rate"), default=0.0)
        state.mwv_rate = _safe_float(row.get("MWV Rate%", ""))
        state.alt_code, _ = FUND_CODE_MAP.get(state.fund_code, (None, None))


    # -------------------------------------------------------------
    # 1) Transaction amounts (Fee / Withdrawal / accum_wd, priorWd, premiums)
    # -------------------------------------------------------------
    @staticmethod
    def get_trxn_amount(state: GMDBState) -> Dict[str, Any]:
        '''
        Get transaction amount and get transaction property
        '''
        txn_type = state.txn_type
        fee = state.txn_amt if txn_type == "ROP Death Benefit Fee" else 0.0
        wd = state.txn_amt if txn_type in WDL_TYPES else 0.0
        prem = state.txn_amt if txn_type == "Initial Premium" else 0.0
        if txn_type == "Reallocation Transfer":
            wd = state.av_end_txn
            prem = state.txn_amt if state.txn_amt > 0 else 0.0
        if txn_type in RESET_TYPES:
            accum_wd = 0.0
            is_withdrawal = False
            priorWd = 0.0
        else:
            # reset cumulative wdl if new year/crediting, else accumulate
            accum_wd = 0.0 if state.prev_txn_type in INDEXVALUATION_TYPES else state.accum_wd + wd
            is_withdrawal = (txn_type in WDL_TYPES)
            priorWd = state.priorWd
        state.accum_wd = accum_wd
        state.is_withdrawal = is_withdrawal
        state.priorWd = wd
        return {"Prem": prem, "Fee": fee, "Wd": wd, "accum_wd": accum_wd, "is_withdrawal": is_withdrawal,
                "priorWd": priorWd}

    # -------------------------------------------------------------
    # 3) Timing (extended boundary + 2nd anniversary logic)
    # -------------------------------------------------------------
    @staticmethod
    def calculate_timing(state: GMDBState) -> Dict[str, Any]:
        """
        Generate boundary flags/properties for later use
        """
        txn_type = state.txn_type
        txn_date = _to_date(state.txn_date)
        cur_pol = state.pol_num
        cur_day = txn_date
        cur_txn = str(state.txn_num)
        cur_fund = state.fund_code

        # boundary flags (policy/day/txn)
        if cur_pol != state.last_pol:
            is_new_policy = True
            is_new_day = True
            is_new_transaction = True
        elif cur_day != state.last_day:
            is_new_policy = False
            is_new_day = True
            is_new_transaction = True
        elif cur_txn != state.last_txn:
            is_new_policy = False
            is_new_day = False
            is_new_transaction = True
        else:
            is_new_policy = is_new_day = is_new_transaction = False

        # new-fund flag
        is_new_fund = (cur_fund != state.last_fund) or is_new_policy or state.prev_txn_type == "Index Valuation" or (state.txn_type == "Free Look Transfer" and state.fund_code != 'H6')
        state.is_freelook = True if (state.txn_type == "Free Look Transfer" and state.fund_code != 'H6') else state.is_freelook

        # deposit flags + count
        is_deposit = (txn_type == "Initial Premium")
        if is_new_policy and is_deposit:
            state.deposit_ct = 1
        elif is_deposit and not is_new_policy:
            state.deposit_ct += 1
        deposit_ct = state.deposit_ct

        # anniversary flags
        is_relocation = (txn_type == "Reallocation Transfer")
        is_anniv = (txn_type in INDEXVALUATION_TYPES)

        # ---- termination flag ----
        if state.prev_txn_type == "Reallocation Transfer" and state.txn_amt == 0:
            is_terminated = True
        else:
            is_terminated = state.is_terminated  # carry forward

        # update boundary context
        state.last_pol = cur_pol
        state.last_day = cur_day
        state.last_txn = cur_txn
        state.last_fund = cur_fund

        # update termination state
        state.is_terminated = is_terminated

        return {
            "is_new_policy": is_new_policy,
            "is_new_day": is_new_day,
            "is_new_transaction": is_new_transaction,
            "is_new_fund": is_new_fund,
            "deposit_ct": deposit_ct,
            "is_relocation": is_relocation,
            "is_anniversary": is_anniv,
            "is_terminated": is_terminated,
        }

    # -------------------------------------------------------------
    # 4) t, dt, bop/eop, pol_yr
    # -------------------------------------------------------------
    @staticmethod
    def calculate_t(state: GMDBState, timing: Dict[str, Any], NEW_FUND_CODE_MAP) -> Dict[str, Any]:
        '''
        Generate/calculate time related items like dt, remaining time in term and various dates
        '''
        eop_date = _to_date(state.txn_date)
        if timing["is_new_policy"] or state.txn_type == "Reallocation Transfer" or (state.txn_type == "Free Look Transfer" and state.fund_code != "H6"):
            bop_date = eop_date
            bot_date = pd.to_datetime(bop_date) if timing["is_new_policy"] or state.txn_type == "Reallocation Transfer" else state.bot_date
        else:
            bop_date = state.bop_date
            bot_date = state.bot_date

        if eop_date and bop_date:
            t = (eop_date - bop_date).days / 365.0
        else:
            t = 0.0
        dt = 0.0 if timing["is_new_policy"] else ((eop_date - state.eop_date).days / 365.0 if state.eop_date and eop_date else 0.0)
        pol_yr = (eop_date.year - bop_date.year) if (eop_date and bop_date) else 0

        state.bop_date = bop_date
        state.eop_date = eop_date
        state.pol_yr = pol_yr
        state.dt = dt
        
        # determining remaining time pct
        txn_date = pd.to_datetime(state.txn_date, format='mixed', errors='coerce')
        _, strat_name = NEW_FUND_CODE_MAP.get(state.fund_code, (None, None))

        if state.fund_code in FIXED_FUND_CODES.union({'H6'}):
            remaining_time_pct = 0.0
            eot_date = eop_date
        else:
            term_years = int(strat_name[0])
            # safe increment by strategy term years, avoid errors from leap year 02/29
            eot_date = bot_date + pd.DateOffset(years=term_years)
            term_days =  eot_date - bot_date if not state.is_freelook else eot_date - pd.to_datetime(bop_date)
            elapsed_days = txn_date - bot_date if not state.is_freelook else txn_date - pd.to_datetime(bop_date)
            remaining_time_pct = 1 - elapsed_days / term_days
        state.remaining_time_pct = remaining_time_pct
        state.bot_date = bot_date
        state.eot_date = eot_date

        return {"t": t, "dt": dt, "bop_date": bop_date, "eop_date": eop_date, "eot_date": eot_date,
                "pol_yr": pol_yr, "remaining_time_pct": remaining_time_pct}

    # -----------------------------
    # crediting time
    # -----------------------------
    @staticmethod
    def calculate_crediting_time(state: GMDBState, timing: Dict[str, Any]) -> bool:
        """
        Determine whether crediting happens at this transaction.
        Returns 
        True or False
        """
        code = state.fund_code
        # if fixed fund and new days passed, credit
        if code in FIXED_FUND_CODES.union({'H6'}) and timing.get("is_new_day", ""):
            crediting_now = True
        # all other strats, wait until Index Valuation to credit
        else:
            crediting_now = bool(timing.get("is_anniversary", False) and state.remaining_time_pct == 0)
        return  crediting_now


    # -----------------------------
    # fund parameter
    # -----------------------------
    @staticmethod
    def get_fund_parameter(row: pd.Series,state: GMDBState, FUND_INDEX_MAP) -> Dict[str, Optional[Any]]:
        """
        Extract a set of parameters depending on which types of strategy rates apply to a fund (FUND_INDEX_MAP)
        Returns:
          {
            "parameter": float|None,
            "parameter_at_term_begin": float|None
          }
        """
        code = state.fund_code
        fix_rate = state.fixed_rate
        freelook_rate = state.freelook_rate/100
        param_val = {}
        if code in FIXED_FUND_CODES:
            param_val.update({"fixed_rate": fix_rate}) 
        elif code == "H6":
            param_val.update({"fixed_rate": freelook_rate}) 
        else:
            rate_dict = FUND_INDEX_MAP.get(code)
            for key, index_str in rate_dict.items():
                if index_str is not None and (not pd.isna(index_str)):
                    if index_str is None or index_str not in row.index:
                        param_val.update({key: None})
                    raw = row.get(index_str)

                    # attach into param_val dict
                    try:
                        rate_val = float(raw)
                    except Exception:
                        token = raw.replace("%", "")
                        try:
                            rate_val = float(token)
                        except Exception:
                            param_val.update({key: None})
                    param_val.update({key: rate_val})
        
        # Concatenate upside parameter info for Annual Statement purpose
        upside_param = " / ".join(k for k in param_val if k not in {"lock_id", "buffer"})
        upside_param_rate = " / ".join(str(v) for k, v in param_val.items() if k not in {"lock_id", "buffer"})
        param_val.update({"upside_param": upside_param})
        param_val.update({"upside_param_rate": upside_param_rate})

        # Update state for next row
        state.parameter = param_val
        return param_val


    # -----------------------------
    # index lookup / return
    # -----------------------------
    @staticmethod
    def _prep_index_df(index_df: pd.DataFrame) -> pd.DataFrame:
        """
        Expect:
            col1 = date
            other columns contain the index series; you referenced col 2,4,5 by position.
        We normalize:
            - Date column named 'INDEX_DATE' (date only)
            - Keep numeric columns only for safety
        """
        if index_df.shape[1] < 2:
            raise ValueError("index.xlsx must have at least 2 columns (date + one index series).")

        idx = index_df.copy()
        # # first column → date
        # idx.iloc[:, 0] = pd.to_datetime(idx.iloc[:, 0], errors="coerce").dt.date
        idx = idx.rename(columns={idx.columns[0]: "INDEX_DATE"})
        # keep rest numeric where possible
        for c in idx.columns[1:]:
            idx[c] = pd.to_numeric(idx[c], errors="coerce")
        return idx


    @staticmethod
    def _lookup_index_value(index_df: pd.DataFrame, the_date, colpos_one_based: Optional[int]) -> Optional[float]:
        """
        Look up value by exact date; if not present, use the last available date <= the_date.
        Returns None if no such date exists or inputs are invalid.
        Assumes index_df['INDEX_DATE'] contains normalized naive timestamps (datetime64[ns]).
        """
        if the_date is None or colpos_one_based is None:
            return None

        # Normalize lookup date to naive midnight Timestamp
        the_ts = pd.to_datetime(the_date, errors="coerce")
        if the_ts is None or pd.isna(the_ts):
            return None
        the_ts = pd.Timestamp(the_ts).tz_localize(None).normalize()

        # Validate column position (1-based; col 0 is INDEX_DATE)
        if not (1 <= colpos_one_based < len(index_df.columns)):
            return None
        series_col = index_df.columns[colpos_one_based]

        # Build a sorted view indexed by date (cheap if already sorted; safe anyway)
        index_df["INDEX_DATE"] = pd.to_datetime(index_df["INDEX_DATE"])
        df_by_date = index_df.set_index("INDEX_DATE").sort_index()
        df_by_date.index = pd.to_datetime(df_by_date.index, errors = "coerce")

        # 1) Exact match fast path
        try:
            val = df_by_date.at[the_ts, series_col]
            return None if pd.isna(val) else float(val)
        except KeyError:
            pass  # no exact date

        # 2) Fallback: last available date <= the_date
        idx = pd.to_datetime(df_by_date.index)
        pos = idx.searchsorted(the_ts, side="right") - 1  # last index <= the_ts
        if pos < 0:  # the_date precedes the first available date
            return None

        val = df_by_date.iloc[pos][series_col]
        return None if pd.isna(val) else float(val)


    @staticmethod
    def _lookup_max_index_value(index_df: pd.DataFrame, start_date, end_date, colpos_one_based: Optional[int]) -> Optional[float]:
        """
        Split a sub series in the index_df based on a start and end date
        Returns the max index level in the time frame, 
        Returns boolean on if the maximum index level is achieved at the txn date (implicit check for lock txn correctness)
        """
        if end_date is None or colpos_one_based is None or start_date is None:
            return None

        # force to datetime
        end_date = pd.to_datetime(end_date, errors="coerce")
        start_date = pd.to_datetime(start_date, errors="coerce")

        # Validate column position (1-based; col 0 is INDEX_DATE)
        if not (1 <= colpos_one_based < len(index_df.columns)) or end_date == start_date:
            return 0, pd.NaT
        
        index_df.iloc[:,0] = pd.to_datetime(index_df.iloc[:,0])
        mask = (index_df.iloc[:,0] >= start_date) & (index_df.iloc[:,0] <= end_date)
        index_series = index_df.loc[mask, index_df.columns[colpos_one_based]]
        max_index_level = index_series.max()
        max_index_date = index_df.iloc[index_series.idxmax(),0]

        return max_index_level, max_index_date

    @classmethod
    def get_index_return(cls,
                         state: GMDBState,
                         index_df: pd.DataFrame,
                         is_new_fund: bool,
                         crediting_now: bool,
                         FUND_EQINDEX_MAP,
                         param: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get index return by comparing txn date index level and term start date index level
        """
        code = state.fund_code
        idx_name = FUND_EQINDEX_MAP.get(code, None)
        lock_id = param.get("lock_id", None)
        if idx_name == "SP500_INDEX":
            colpos = 1
        elif idx_name == "NAS_DQ":
            colpos = 3
        else:
            colpos = None
        txn_type = state.txn_type
        txn_date = _to_date(state.txn_date)

        # empty and fixed fund handling
        if idx_name is None or colpos is None or code in FIXED_FUND_CODES.union({'H6'}):
            state.prev_crediting_now = bool(crediting_now)
            return {
                "index_value": state.last_index_value,
                "index_name": None,
                "index_return": None,
                "last_index_value": state.last_index_value,
            }

        # 1) Determine current_val
        if not state.manual_locked and (is_new_fund or (txn_type in RESET_TYPES.union(INDEXVALUATION_TYPES))):
            current_val = cls._lookup_index_value(index_df, txn_date, colpos)
            did_lookup = True
        elif txn_type in {"Manual Performance Capture"} and lock_id == 1.0 :
            current_val = state.prev_index_value
            did_lookup = False
            state.manual_locked = True
        elif txn_type in {"Lock Threshold Achieved", "Auto Performance Capture"} and lock_id == 1.0 :
            current_val = state.prev_index_value
            did_lookup = False
            state.auto_locked = True
        else:
            current_val = state.prev_index_value
            did_lookup = False

        # 2) Update last_index_value
        if is_new_fund:
            new_last = current_val
        elif getattr(state, "prev_crediting_now", False):
            new_last = state.prev_index_value
        else:
            new_last = state.last_index_value

        # 3) Compute return using new_last
        if current_val is None or new_last in (None, 0):
            idx_ret = None
        elif crediting_now:
            idx_ret = (current_val / new_last) - 1.0
        else:
            idx_ret = None

        # 4) BOP index tracking
        if txn_type in {"Initial Premium", "Rate Renewal"}:
            bop_index_value = cls._lookup_index_value(index_df, state.bop_date, colpos)
        else:
            bop_index_value = state.bop_index_value

        # 5) Update state
        state.index_name = idx_name
        state.last_index_value = new_last
        if did_lookup:
            state.prev_index_value = current_val
        state.last_index_return = idx_ret
        if (txn_type == "Annual Processing" and state.eot_date == state.txn_date) or txn_type == "Reallocation Transfer":
            state.manual_locked = state.auto_locked = False
        state.prev_crediting_now = bool(crediting_now)
        state.bop_index_value = bop_index_value


        return {
            "index_name": idx_name,
            "index_value": current_val,
            "last_index_value": new_last,
            "index_return": idx_ret,
            "bop_index_value": bop_index_value
        }
    @classmethod
    def get_max_index_return(cls,
                         state: GMDBState,
                         index_df: pd.DataFrame,
                         is_new_fund: bool,
                         param: Dict[str, Any],
                         FUND_EQINDEX_MAP) -> Dict[str, Any]:
        """
        for locked funds only, tracks the maximum index return between term start date to current txn date
        """
        code = state.fund_code
        if code not in LOCK_FUNDS:
            return {"max_index_level":0, "correct_lock":False}
        lock_level = param.get("lock")
        

        idx_name = FUND_EQINDEX_MAP.get(code, None)
        # lock_id = param.get("lock_id", None)
        if idx_name == "SP500_INDEX":
            colpos = 1
        elif idx_name == "NAS_DQ":
            colpos = 3
        else:
            colpos = None
        txn_type = state.txn_type
        txn_date = _to_date(state.txn_date)
        

        # 1) Determine starting equity index level
        if not state.auto_locked and (is_new_fund or (txn_type in RESET_TYPES.union(INDEXVALUATION_TYPES))):
            current_val = cls._lookup_index_value(index_df, txn_date, colpos)
        else:
            current_val = state.prev_index_value

        # 2) Calculate max historical index gain on txn date (if date later than term start)
        if is_new_fund:
            max_index_level = 0
            max_index_date = None
        else:
            max_index_level, max_index_date = cls._lookup_max_index_value(index_df, state.bop_date, txn_date, colpos)
            max_index_gain = max_index_level / current_val - 1

        # 3) Determines whether the current txn date's position is correct given when the max_return is achieved
        if max_index_date is None and max_index_level == 0:
            correct_lock = False
        elif max_index_gain >= lock_level/100 and max_index_date == pd.to_datetime(state.txn_date) and state.auto_locked:
            correct_lock = True
        else:
            correct_lock = False


        return {
            "max_index_level": max_index_level,
            "correct_lock": correct_lock,
        }

    @classmethod
    def get_strat_base(cls, state: GMDBState, 
                       timing: Dict[str, Any], trxn: Dict[str, Any], trading_cost: float) -> float:
        is_wdl = trxn.get("is_withdrawal")
        is_anniv = timing.get("is_anniv", "")
        code = state.fund_code
        txn_type = state.txn_type
        
        # Unique locked lock fund handling and direct return
        if state.auto_locked and code in LOCK_FUNDS:
            # prorated reduction upon withdrawawl, no OUV element
            if txn_type in WDL_TYPES:
                strat_base = (state.av_end_txn - state.txn_amt) / state.av_end_txn * state.strat_base
            elif (is_anniv or txn_type in {"Reallocation Transfer", "Initial Premium", "Free Look Transfer"}):
                strat_base = state.fund_bal
            else:
                strat_base = state.strat_base
            state.strat_base = strat_base
            return strat_base
                
        # for initial premium or crediting point or lock triggered lock fund, reset strat base to AV
        if (is_anniv or txn_type in {"Reallocation Transfer", "Initial Premium", "Free Look Transfer"}):
            strat_base = state.fund_bal
        # fixed fund / in freelook handling 
        elif code in FIXED_FUND_CODES.union({'H6'}):
            strat_base = 0.0
        # all else, mainly withdrawals / fees, and no change
        else:
            if state.av_end_txn == 0 and state.strat_base == 0:
                state.fund_portion = 0
                return 0
            if state.remaining_time_pct == 0:
                av_interim_before_wdl = state.av_end_txn
            else:
                av_interim_before_wdl = state.strat_base * (1 - state.bop_ouv * state.remaining_time_pct + state.eop_ouv - trading_cost)
            sys_pol_IV_before_WDL = state.sys_pol_iv - state.pol_gross_wd
            state.fund_portion = av_interim_before_wdl / sys_pol_IV_before_WDL
            state.fund_gross_wd = state.pol_gross_wd * state.fund_portion
            state.av_interim_before_wdl = av_interim_before_wdl
            # withdrawal + non term end ROP fee handling - OUV adjusted
            if is_wdl or (txn_type == "ROP Death Benefit Fee" and state.remaining_time_pct > 0.0):
                strat_base = state.strat_base * (av_interim_before_wdl + state.fund_gross_wd) / av_interim_before_wdl
            # Term end ROP fee handling - D4D reduction
            elif txn_type == "ROP Death Benefit Fee" and state.remaining_time_pct == 0.0:
                strat_base = state.strat_base + state.txn_amt
            # all else strat base unchanged
            else:
                strat_base = state.strat_base

        state.strat_base = strat_base
        return strat_base
    
    # -----------------------------
    # Entire OUV query output caching for faster lookup
    # -----------------------------
    @staticmethod
    def _prep_ouv_df(ouv: pd.DataFrame) -> pd.DataFrame:
        """
        Formating some values and column # checking
        """
        if ouv.shape[1] < 2:
            raise ValueError("OUV_Values.csv must have at least 2 columns (date + one index series).")
        df = ouv.copy()

        # Force date fields to pd dates
        date_cols = ['PO_TERM_START_DATE', 'PO_VALUATION_DATE', 'PO_TERM_END_DATE']
        for col in date_cols:
            df[col] = pd.to_datetime(df[col]).dt.date
            # df[col] = df[col].dt.strftime('%m/%d/%Y')

        # Convert the string fields to be string objects
        str_cols = ['PO_FUND_CODE', 'PO_FUND_NAME']
        for col in str_cols:
            df[col] = df[col].astype('string')

        # set index for later fast search
        df = df.set_index(['PO_TERM_START_DATE', 'PO_FUND_CODE', 'PO_VALUATION_DATE']).sort_index()
        
        # standardize field names for later lookup
        df = df.rename(columns={'PO_BUFFER': 'buffer', 
                                'PO_CAP': 'cap', 
                                'PO_PART_RATE': 'par',
                                'PO_TRIGGER_RATE': 'trig', 
                                'PO_LOCK_RATE': 'lock'})
        return df

    @staticmethod
    def _lookup_ouv_value(index_rate, bop_date, strat_code, txn_date, ouv_df) -> float:
        """
        lookup ouv value for current row based on UniqueIndex's Index Rate, Term Start date, Strategy Name, Txn/current date 
        """
        # 1. Force dates to nearest business day
        bop_date = us_bus.rollback(bop_date).date()
        txn_date = us_bus.rollback(txn_date).date()

        # 2. force dates to string for lookup
        # bop_date = bop_date.strftime("%m/%d/%Y")
        # txn_date = txn_date.strftime("%m/%d/%Y")

        # 3. Filter the big ouv df down to a sub ouv df
        try:
            sub_ouv_df = ouv_df.loc[(bop_date, strat_code, txn_date)]
        except KeyError:
            try:
                # Handle anniversary OUV unavailable, move transaction date 1 day before
                txn_date = txn_date - pd.Timedelta(days=1)
                sub_ouv_df = ouv_df.loc[(bop_date, strat_code, txn_date)]
            except KeyError:
                ouv_val = None  # Handle cases where the combination doesn't exist
                return ouv_val
        
        # 4. Further filtering based on dynamically available index rate types-values pairs
        mask = True
        for key, value in index_rate.items():
            if key in sub_ouv_df.columns:
                mask &= (sub_ouv_df[key] == value)
        ouv_val = sub_ouv_df[mask]

        # 5. Extract OUV field value from the single line mask result
        if not ouv_val.empty:
            ouv_val = float(ouv_val['PO_OUV'].iloc[0])
        else:
            ouv_val = 0.0

        return ouv_val

    @classmethod  
    def lookup_ouv_value(cls, state: GMDBState, ouv_df: pd.DataFrame, timing) -> Dict:
        """
        set eop bop rules based on txn_type or is_new_fund flag
        """
        alt_code = state.alt_code
        index_rate_ouv_lookup = state.parameter
        # Only extract index rate dictionary for lookup once during the fund's lifetime, until next reallocation
        if state.txn_type in {"Reallocation Transfer", "Initial Premium", "Free Look Transfer"}:
            index_rate_ouv_lookup = {k: v for k, v in index_rate_ouv_lookup.items() if k in {'buffer', 'cap', 'par', 'trig', 'lock'}}
            state.index_rate_ouv_lookup = index_rate_ouv_lookup
        else:
            index_rate_ouv_lookup = state.index_rate_ouv_lookup

        bop_date = state.bop_date
        # freelook handling
        if state.txn_type == 'Free Look Transfer' and state.fund_code != 'H6':
            bop_date = state.txn_date

        # empty / fixed fund / no txn date movement handling
        if (alt_code is None or state.fund_code in FIXED_FUND_CODES.union({'H6'}) or
            (state.fund_code in LOCK_FUNDS and state.auto_locked)):
            return {
                "eop_ouv": state.eop_ouv,
                "bot_ouv": state.bop_ouv,
            }

        # update bop_ouv or eop_ouv depending on transaction type / if new strategy fund
        if timing.get("is_new_fund"):
            bop_ouv = cls._lookup_ouv_value(index_rate_ouv_lookup, bop_date, alt_code, state.txn_date, ouv_df)
            eop_ouv = bop_ouv
        else:
            bop_ouv = state.bop_ouv
            eop_ouv = cls._lookup_ouv_value(index_rate_ouv_lookup, bop_date, alt_code, state.txn_date, ouv_df)

        # Update state
        state.eop_ouv = eop_ouv
        state.bop_ouv = bop_ouv

        return {
            "eop_ouv": state.eop_ouv,
            "bop_ouv": state.bop_ouv,
        }
    
    @classmethod  
    def lookup_max_ouv_value(cls, state: GMDBState, ouv_df: pd.DataFrame, 
                             al_funds_df: pd.DataFrame, timing: Dict[str, Any], 
                             trading_cost: float) -> Dict:
        """
        For auto perf capture preset funds only
        Tracks the maximum ouv gain between term start date to current txn date
        Returns the maximum OUV value during the dates, and a boolean value that indicates whether a capture happens now or not
        """
        code = state.fund_code
        alt_code = state.alt_code
        index_rate_ouv_lookup = state.index_rate_ouv_lookup

        # get auto capture level (only at fund start), if not eligible return empty
        if timing["is_new_fund"] and (state.pol_num in al_funds_df.iloc[:, 0].values and code in al_funds_df.iloc[:, 4].values):
            state.capture_level = al_funds_df.loc[(al_funds_df['AL_ACC_CODE'] == code) & (al_funds_df['PC_CONT'] == state.pol_num), 'AL_ALLOC_AMT'].max()
            state.auto_capture_funds = al_funds_df.loc[(al_funds_df['PC_CONT'] == state.pol_num), 'AL_ACC_CODE'].drop_duplicates().values
        # exit if policy not in AL list, or if policy is in, but the code is not in AL list
        elif state.pol_num not in al_funds_df.iloc[:, 0].values or (code not in state.auto_capture_funds):
            return {
                "max_ouv_gain": 0,
                "correct_capture": False,
            }
        capture_level = float(state.capture_level)

        # empty / fixed fund / no txn date movement handling
        if alt_code is None or state.fund_code in FIXED_FUND_CODES.union({'H6'}) or state.lock_credited:
            return {
                "max_ouv_gain": 0,
                "correct_capture": False,
            }   

        # freelook start date shift handling
        if state.txn_type == 'Free Look Transfer' and state.fund_code != 'H6':
            bop_date = state.txn_date
        else:
            bop_date = state.bop_date
        
        # 1) Determine starting ouv level
        if not state.auto_locked and (timing["is_new_fund"] or (state.txn_type in RESET_TYPES.union(INDEXVALUATION_TYPES))):
            bop_ouv = cls._lookup_ouv_value(index_rate_ouv_lookup, bop_date, alt_code, state.txn_date, ouv_df)
        else:
            bop_ouv = state.bop_ouv

        # 2) Calculate max historical index gain on txn date (if date later than term start)
        if timing.get("is_new_fund"):
            max_ouv_level = 0
            max_ouv_gain = 0
            max_ouv_date = pd.NaT
        else:
            max_ouv_level, max_ouv_date = cls._lookup_max_ouv_value(ouv_df, state.bop_date, state.txn_date, alt_code)
            max_ouv_gain = max_ouv_level - bop_ouv * state.remaining_time_pct - trading_cost

        # 3) Determines whether the current txn date's position/type is correct given when the max_return is achieved
        if pd.isna(max_ouv_date) and max_ouv_level == 0:
            correct_capture = False
        elif max_ouv_gain >= capture_level/100 and max_ouv_date == state.txn_date and state.auto_locked:
            correct_capture = True
        else:
            correct_capture = False


        return {
            "max_ouv_gain": max_ouv_gain,
            "correct_capture": correct_capture,
        }

    @staticmethod
    def _lookup_max_ouv_value(ouv_df: pd.DataFrame, start_date, end_date, strat_code: str) -> Optional[float]:
        """
        Split a sub series in the index_df based on a start and end date
        Returns the max index level in the time frame, 
        Returns boolean on if the maximum index level is achieved at the txn date (implicit check for lock txn correctness)
        """
        if end_date is None or start_date is None or end_date == start_date:
            return 0, pd.NaT
        
        # 1. Force dates to nearest business day
        start_date = us_bus.rollback(start_date).date()
        end_date = us_bus.rollback(end_date).date()

        # 2. force dates to string for lookup
        # start_date = start_date.strftime("%m/%d/%Y")
        # end_date = end_date.strftime("%m/%d/%Y")

        # 3. Filter the big ouv df down to a sub ouv df
        try:
            sub_ouv_df = ouv_df.loc[(start_date, strat_code, slice(start_date, end_date))]
        except KeyError:
            try:
                # Handle anniversary OUV unavailable, move transaction date 1 day before
                end_date = end_date - pd.Timedelta(days=1)
                sub_ouv_df = ouv_df.loc[(start_date, strat_code, slice(start_date, end_date))]
            except KeyError:
                return 0, pd.NaT
        
        # 4. get the max of OUV and the date of max during the time frame
        max_ouv_level = float(sub_ouv_df["PO_OUV"].max())
        max_ouv_date = sub_ouv_df["PO_OUV"].idxmax()[2]  # get the third index, which is the txn date


        return max_ouv_level, max_ouv_date



    # -----------------------------
    # Entire VIX output caching for faster lookup
    # -----------------------------
    @staticmethod
    def _prep_vix_df(vix: pd.DataFrame) -> pd.DataFrame:
        """
        Formating some values and column # checking
        """
        if vix.shape[1] != 3:
            raise ValueError("VX_Values.csv must have 3 columns. Please check data file.")
        df = vix.copy()
        # force the second col date time to be string (for later lookup)
        df.iloc[:, 1] = df.iloc[:, 1].apply(pd.to_datetime, errors='coerce')

        # force the third col to numeric if not already so
        df.iloc[:, 2] = pd.to_numeric(df.iloc[:, 2], errors='coerce')

        return df
    
    @staticmethod
    def _prep_alfunds_df(al_funds: pd.DataFrame) -> pd.DataFrame:
        """
        Formating some values and column # checking
        """
        df = al_funds.copy()

        # force below columns to pd datetime object
        cols_to_datetime = [1, 3, 7, 8, 9]
        for col in cols_to_datetime:
            df.iloc[:, col] = pd.to_datetime(df.iloc[:, col], errors='coerce')

        # # force the 6th col to numeric if not already so
        # df.iloc[:, 5] = pd.to_numeric(df.iloc[:, 2], errors='coerce')

        return df
    
    @classmethod
    def lookup_vix_tc_value(cls, state: GMDBState, vix_df: pd.DataFrame, ouv: pd.DataFrame) -> float:
        """
        lookup vix level by txn date and then apply logic to get transaction cost
        """
        txn_date = _to_date(state.txn_date)
        txn_date = us_bus.rollback(txn_date).date()
        txn_date = txn_date.strftime("%m/%d/%Y")
        eop_ouv = ouv.get("eop_ouv", "")
        
        vix_df['RV_RATE_DATE'] = pd.to_datetime(vix_df['RV_RATE_DATE'])
        vix_indexed = vix_df.set_index('RV_RATE_DATE').sort_index()
        
        try:
            vix_val = vix_indexed.loc[txn_date, 'RV_RATE_VALUE']
        except KeyError:
            try:
                txn_date = _to_date(txn_date) - timedelta(days = 1)
                txn_date = txn_date.strftime("%m/%d/%Y")
                vix_val = vix_indexed.loc[txn_date, 'RV_RATE_VALUE']
            except KeyError:
                vix_val = None
        
        if eop_ouv is None or state.fund_code in FIXED_FUND_CODES.union({'H6'}):
            return 0
        else:
            # Exception funds that use different multipliers
            if state.fund_code in {"RO", "RN", "RM"}:
                tc_val = eop_ouv * 0.12 if vix_val > 30 else eop_ouv * 0.03
            # regular multiplier set
            else:
                tc_val = eop_ouv * 0.04 if vix_val > 30 else eop_ouv * 0.01
            return tc_val

    # -----------------------------
    # strategy return by fund code
    # -----------------------------
    @classmethod
    def calculate_strategy_return(cls, state: GMDBState, index_return: Optional[float], 
                                  parameter: Optional[float], max_idx_info: Dict[str, Any],
                                  max_ouv: Dict[str, Any]) -> float:
        """
        Case logic per spec. Units:
          - index_return is decimal (e.g., 0.04 for +4%).
          - parameter is percentage in whole units (e.g., 5 for 5%). Convert to decimal with /100 when needed.
        """

        code = state.fund_code
        # fix_rate = _safe_float(row.get("FIX RATE"), default=0.0)
        r = 0.0
        base = 0.0 if index_return is None else float(index_return)
        buffer = parameter.get("buffer")
        par = parameter.get("par")
        cap = parameter.get("cap")
        lock = parameter.get("lock")
        trig = parameter.get("trig")
        buffer = buffer / 100 if buffer is not None else None
        par = par / 100 if par is not None else None
        cap = cap / 100 if cap is not None else None
        lock = lock / 100 if lock is not None else None
        trig = trig / 100 if trig is not None else None

        if max_ouv["correct_capture"] and code in state.auto_capture_funds:
            return max_ouv["max_ouv_gain"]

        # fixed fund + Freelook fund
        if code in FIXED_FUND_CODES.union({'H6'}):
            fixed = parameter.get("fixed_rate")
            r = fixed
        # Buffer with Participation funds
        elif code in {"RW", "RV"}:
            if base < 0:
                r = min(0, base + buffer)
            else:
                r = base
        # Buffer and Cap funds
        elif code in {"RB", "RC", "RD", "RG", "RH "}:
            if base < 0:
                r = min(0, base + buffer)
            else:
                r = min(base, cap)
        # Dual Direction wCap funds
        elif code in {"RJ", "RI", "RK", "RL"}:
            if base < 0:
                if abs(base) <= buffer:
                    r = abs(base)
                else:
                    r = min(0, base + buffer)
            else:
                r = min(base, cap)
        # Buffer with Par & Cap funds
        elif code in {"TC", "TD"}:
            if base < 0:
                r = min(0, base * par + buffer)
            else:
                r = min(base * par, cap)
        elif code in LOCK_FUNDS:
            if max_idx_info.get("correct_lock", False) and state.auto_locked:
                r = lock
            elif base <= 0:
                r = min(0, base + buffer)
        # Buffer with Trigger
        elif code in {"RE", "RF"}:
            if base > 0:
                r = trig
            elif base <= 0:
                r = min(0, base + buffer)
        # Dual Direction Trigger Cap
        elif code in {"TL"}:
            r = trig
        else:
            # default: just pass through the index return if present
            r = float(index_return) if index_return is not None else 0.0

        r = round(r, 4)
        return r

    # -----------------------------
    # AV rollforward
    # -----------------------------
    @staticmethod
    def calculate_av(state: GMDBState, strategy_return: float, t_info: Dict[str, Any],
                     ouv: Dict[str, Any], max_ouv: Dict[str, Any], trading_cost: float) -> Dict[str, float]:        
        """
        Put description here
        """
        txn_type = state.txn_type
        code = state.fund_code
        txn_amt = state.txn_amt

        dt = t_info.get("dt")
        remaining_time_pct = t_info.get("remaining_time_pct")

        eop_ouv = (ouv.get("eop_ouv", "") or 0)
        bop_ouv = (ouv.get("bop_ouv", "") or 0)
        
        av_pre_txn = 0.0 if state.av_end_txn is None else state.av_end_txn
        interest_credit_amt = 0.0
        # Initial premium / Freelook transfer target funds - get system fund balance 
        if (txn_type in {"Initial Premium", "Reallocation Transfer"} or 
            (txn_type == "Free Look Transfer" and code != "H6")):
            av_end_txn = state.fund_bal
        # normal end of term crediting
        elif txn_type == "Index Valuation" and remaining_time_pct == 0.0 and not state.manual_locked and not state.auto_locked:
            interest_credit_amt = (state.strat_base * float(strategy_return))
            av_end_txn = state.strat_base + interest_credit_amt
        # auto lock (lock funds only)
        elif txn_type == "Lock Threshold Achieved" and state.auto_locked  and not state.lock_credited:
            interest_credit_amt = (state.strat_base * float(strategy_return))
            av_end_txn = state.strat_base + interest_credit_amt
        # auto performance capture (all except lock funds)
        elif txn_type == "Auto Performance Capture" and state.auto_locked and not state.lock_credited:
            interest_credit_amt = (state.strat_base * float(strategy_return))
            av_end_txn = state.strat_base + interest_credit_amt
        # processing/dummy txn types + withdrawals (prorated deduction embedded in strat base already) 
        # + manual performance capture + Index Valuation but not on this particular fund
        elif (txn_type in WDL_TYPES.union({"Processing Date", "Annual Processing", "Manual Performance Capture", "Account Value Adjustment", 
                                           "Death Benefit", "Lock Threshold Achieved", "Auto Performance Capture"}) and
                (code not in LOCK_FUNDS or not state.lock_credited) or
                (txn_type in {"Index Valuation","ROP Death Benefit Fee"} and remaining_time_pct > 0.0)):
            # calculate OUV adjusted AV a.k.a Interim Value
            if remaining_time_pct == 0.0 or state.prev_txn_type in {"Index Valuation", "Reallocation Transfer"}:
                av_end_txn = av_pre_txn
                state.strat_base = av_end_txn
            else:
                av_end_txn = state.strat_base * (1 + eop_ouv - bop_ouv * remaining_time_pct - trading_cost)
        elif txn_type in {"ROP Death Benefit Fee", "Reallocation Transfer"} and remaining_time_pct == 0.0:
            av_end_txn = av_pre_txn + txn_amt + state.fee * state.fund_portion            
        else:
            av_end_txn = av_pre_txn
            

        # Fixed or locked fund post-lock fixed rate crediting
        if (txn_type != "Manual Performance Capture" and (code in FIXED_FUND_CODES.union({'H6'}) or state.manual_locked) or
            (state.lock_credited and state.auto_locked)):
            strategy_return = 0.01 if state.manual_locked or state.auto_locked else strategy_return
            if dt != 0:
                interest_credit_amt = max(0.0, av_pre_txn * ((1 + strategy_return) ** dt - 1.0))
            else:
                interest_credit_amt = 0.0
            av_end_txn = av_pre_txn + interest_credit_amt + txn_amt
            av_end_txn = 0.0 if av_end_txn < 0.5 else av_end_txn

        # flag to track fund specific lock crediting history
        if ((txn_type == "Lock Threshold Achieved" and state.auto_locked and code in LOCK_FUNDS and not state.lock_credited) or
            (txn_type == "Auto Performance Capture" and state.auto_locked and not state.lock_credited)):
            state.lock_credited = True

        # update state for next row
        state.av_end_txn = av_end_txn
        return {
            "av_pre_txn": av_pre_txn,
            "interest_credit_amt": interest_credit_amt,
            "av_end_txn": av_end_txn,
        }


    # -------------------------------------------------------------
    # 9) MWV (Minimum Withdrawal Value)
    # -------------------------------------------------------------
    @staticmethod
    def calculate_mwv(row: pd.Series, state: GMDBState, 
                      av: Dict[str, Any], timing: Dict[str, Any], 
                      strat_r: float, trading_cost: float, surr_charge: float) -> Dict[str, Any]:
        av_eop = _safe_float(av.get("av_end_txn"))
        av_bop = _safe_float(av.get("av_pre_txn"))
        dt = state.dt
        Wd = state.txn_amt if state.txn_type in WDL_TYPES else 0
        txn_type = state.txn_type
        code = state.fund_code
        eop_ouv = state.eop_ouv
        bop_ouv = state.bop_ouv

        #initialize to system value (dynamic field name referencing)
        if (timing["is_new_policy"] or 
            txn_type in {"Reallocation Transfer", "Initial Premium"} or 
            (txn_type == "Free Look Transfer" and state.fund_code != "H6")):
            mwv_base = row.get("MWV_BASE" + state.fund_code)
            mwv = mwv_base
        # Freelook handling for freelook holding account, zeroing
        elif txn_type == "Free Look Transfer" and state.fund_code == "H6":
            mwv = 0.0
            mwv_base = 0.0
        # lock funds and auto capture funds handling 
        elif ((code in LOCK_FUNDS or code in state.auto_capture_funds) and state.auto_locked):
            mwv = av_eop * 0.875
            mwv_base = state.mwv_base
        # fixed fund handling, grows at SNFL rate
        elif code in FIXED_FUND_CODES.union({"H6"}):
            mwv = (state.mwv * ((1.0 + state.mwv_rate) ** dt)) + Wd
            mwv_base = state.mwv_base
        # Bulk of txn types handling, adjusted by OUV implictly via AV, not at term end
        elif txn_type in ("Processing Date", "Annual Processing", "Account Value Adjustment", 
                          "Death Benefit", "Lock Threshold Achieved", "Auto Performance Capture",
                          "Manual Performance Capture", "Index Valuation") and state.remaining_time_pct > 0.0:
            mwv = av_eop / av_bop * state.mwv if av_bop != 0.0 else 0.0
            mwv_base = state.mwv_base
        # non-lock funds and manual capture handling
        elif code not in LOCK_FUNDS and state.manual_locked:
            mwv = (state.mwv * ((1.0 + 0.01) ** dt)) + Wd
            mwv_base = state.mwv_base
        # Term end crediting handling
        elif txn_type == "Index Valuation" and state.remaining_time_pct == 0.0:
            mwv = (1 + strat_r) * state.mwv_base
            mwv_base = mwv
        # prorated net withdrawal deducted from mwv based on fund level iv / policy level iv
        elif txn_type in WDL_TYPES:
            mwv_before_wdl = state.mwv_base * (1 + eop_ouv - bop_ouv * state.remaining_time_pct - trading_cost)
            net_wd = state.pol_gross_wd * state.fund_portion - surr_charge
            mwv = mwv_before_wdl + net_wd
            mwv_base = state.mwv_base * mwv / mwv_before_wdl if mwv_before_wdl != 0.0 else 0.0
        elif txn_type == "ROP Death Benefit Fee":
            if state.remaining_time_pct == 0.0:
                mwv = state.mwv + state.fee * state.fund_portion
                mwv_base = state.mwv_base + state.fee * state.fund_portion
            else:
                mwv_before_wdl = state.mwv_base * (1 + eop_ouv - bop_ouv * state.remaining_time_pct - trading_cost)
                # backed out system value for policy level interim value right before withdrawal
                sys_pol_av_interim_before_fee = row.get("ACCOUNT_VAL") - state.pol_gross_wd
                # prorated net withdrawal deducted from mwv based on fund level iv / policy level iv
                mwv = mwv_before_wdl + (state.pol_gross_wd * state.av_interim_before_wdl / sys_pol_av_interim_before_fee)
                mwv_base = state.mwv_base * mwv / mwv_before_wdl if mwv_before_wdl != 0.0 else 0.0
        else:
            mwv = state.mwv
            mwv_base = state.mwv_base

        # update state
        state.mwv = mwv
        state.mwv_base = mwv_base
        state.prev_txn_type = txn_type

        return {"mwv_interim": mwv, "mwv_base": mwv_base}
    
    @staticmethod
    def calculate_AS_txn_amt(state: GMDBState) -> float:
        if state.txn_type in WDL_TYPES:
            AS_txn_amt = state.txn_amt
        elif state.txn_type == 'ROP Death Benefit Fee':
            AS_txn_amt = state.fee * state.fund_portion
        elif state.txn_type in ("Manual Performance Capture", "Auto Performance Capture", "Lock Threshold Achieved"):
            AS_txn_amt = state.av_end_txn
        elif state.txn_type == "Reallocation Transfer":
            AS_txn_amt = -state.av_end_txn
        else:
            return None

        return AS_txn_amt
    # -------------------------------------------------------------
    # 10) Load / transform surrender charge table to nested dict
    # -------------------------------------------------------------
    @staticmethod
    def load_surr_chrg_table_to_dict(surr_file_path: str, results_basepath: str) -> Dict[str, Any]:
        df = pd.read_csv(surr_file_path)

        # Normalize expected columns
        exp = ["ADMIN_SYSTEM", "STATE_CD", "PROD_DESC", "PROD_DESC_CD", "YR_NUM", "SURR_CHRG_RATE"]
        for c in exp:
            if c not in df.columns:
                raise KeyError(f"Surrender file missing required column: {c}")

        # Build nested dict with PROD_DESC_CD and YR_NUM using 0-based indices
        out: Dict[str, Any] = {}
        for (admin, state_cd, prod_desc), g in df.groupby(["ADMIN_SYSTEM", "STATE_CD", "PROD_DESC"], dropna=False):
            admin_key = str(admin)
            state_key = str(state_cd)
            prod_key = str(prod_desc).upper()

            out.setdefault(admin_key, {})
            out[admin_key].setdefault(state_key, {})
            out[admin_key][state_key].setdefault(prod_key, {})

            # index PROD_DESC_CD
            for pidx, (pval, gp) in enumerate(g.groupby("PROD_DESC_CD", dropna=False)):
                out[admin_key][state_key][prod_key].setdefault(pidx, {})
                # index YR_NUM within this prod_desc_cd group
                yrs = gp.sort_values(by="YR_NUM", kind="mergesort")
                for yidx, (_, row2) in enumerate(yrs.iterrows()):
                    out[admin_key][state_key][prod_key][pidx][yidx] = _safe_float(row2.get("SURR_CHRG_RATE"))

        # Save to JSON
        os.makedirs(results_basepath, exist_ok=True)
        out_path = os.path.join(results_basepath, "surr_chrg_table_navisys_RILA.json")
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)

        return out

    # -------------------------------------------------------------
    # 11) Lookup surrender charge rate
    # -------------------------------------------------------------
    @staticmethod
    def get_surrender_chrg(row: pd.Series, state: GMDBState, surr_dict: Dict[str, Any]) -> float:
        """
        Look up surrender charge rate from nested dictionary surr_dict.
        Keys: ADMIN_SYSTEM -> STATE_CD -> PROD_DESC (uppercased) -> PROD_DESC_CD idx -> YR_NUM idx
        """
        # map policy year -> 0-based index
        pol_idx = max(0, int(state.pol_yr))

        admin = "NAVISYS"
        st = "NS"
        prod = str(row.get("PL_PRD_DESC") or "").upper()  # Upp case product name
        prod_cd_idx = 0  # always 0 per spec

        try:
            surr_rate = surr_dict[admin][st][prod][prod_cd_idx][pol_idx]
        except Exception:
            return 0.0
        txn_type = state.txn_type
        pol_gross_wdl = row.get("GrWd+PErecap")     # Policy level gross withdrawal
        sys_pol_IV_amt = row.get("ACCOUNT_VAL")     # Policy level account value
        # refill fwv amount annually
        if txn_type in ANNUAL_REFRESH_TYPES:
            pol_fwv = sys_pol_IV_amt * 0.1
            surr_charge = 0.0
        # reduce fwv amount at withdrawals
        elif txn_type in WDL_TYPES:
            pol_fwv = max(0, state.fwv + pol_gross_wdl)
            surr_charge = surr_rate * max(0, state.pol_gross_wd -pol_fwv) * state.fund_portion
            state.fund_net_wd = state.fund_gross_wd + surr_charge 
        else:
            surr_charge = 0.0
            pol_fwv = state.fwv

        state.fwv = pol_fwv
        return surr_charge

    @classmethod
    def aggregate(cls, states: "dict[tuple[str,str], GMDBState]", policy_num: str, txn_num: str) -> dict:
        """
        Sum current fund-level values across all funds for a given policy & transaction number.
        We use GMDBState.last_txn (string) to identify which rows belong to the given txn.
        Returns a dict with totals; if nothing to sum, returns zeros.
        """
        total_fund_value = 0.0
        found_any = False

        for (pol, fund), st in states.items():
            if pol != policy_num:
                continue
            # Only include fund states that are currently at the txn we want to aggregate
            if _to_str(getattr(st, "av_end_txn", "")) != _to_str(txn_num):
                continue

            # Use the latest end-of-period values tracked in GMDBState
            av_end_txn = float(getattr(st, "av_end_txn", 0.0) or 0.0)

            total_fund_value += av_end_txn
            found_any = True

        if not found_any:
            return {
                "total_fund_value": 0.0
            }

        return {
            "total_fund_value": total_fund_value
        }

    # -----------------------------
    # Per-row processing
    # -----------------------------
    @classmethod
    def process_row(cls, row: pd.Series, state: GMDBState, 
                    index_df: pd.DataFrame, ouv_df: pd.DataFrame, vix_df: pd.DataFrame, al_funds_df: pd.DataFrame,
                    surr_chrg_table: Dict[str, Any],
                    last_aggregate: pd.Series) -> Dict[str, Any]:
        
        # pre-assign some state variables
        cls.prepopulate_state(row, state, cls.FUND_CODE_MAP)

        # transaction properties; timing + boundaries; t/dt/policy-year, boundary conditions
        trxn = cls.get_trxn_amount(state)
        timing = cls.calculate_timing(state)
        t_info = cls.calculate_t(state, timing, cls.FUND_CODE_MAP)  # updates bop/eop/pol_yr

        # parameter / index / strategy / crediting
        crediting_now = cls.calculate_crediting_time(state, timing)
        param = cls.get_fund_parameter(row, state, cls.FUND_INDEX_MAP)
        idx_info = cls.get_index_return(state, index_df, timing.get("is_new_fund"), crediting_now, cls.FUND_EQINDEX_MAP, param)
        max_idx_info = cls.get_max_index_return(state, index_df, timing.get("is_new_fund"), param, cls.FUND_EQINDEX_MAP)

        # RILA Specific
        ouv = cls.lookup_ouv_value(state, ouv_df, timing)                        # EOP and BOP OUV lookup
        trading_cost = cls.lookup_vix_tc_value(state, vix_df, ouv)                                  # Trading cost determined by VIX
        max_ouv = cls.lookup_max_ouv_value(state, ouv_df, al_funds_df, timing, trading_cost)   # Get max value of OUV from term start date to txn date
        strat_base = cls.get_strat_base(state, timing, trxn, trading_cost)                          # Tracking Strategy base

        # AV rollforward
        strat_r = cls.calculate_strategy_return(state, idx_info.get("index_return"),param, max_idx_info, max_ouv)
        av = cls.calculate_av(state, strat_r, t_info, ouv, max_ouv, trading_cost)
        strat_base = state.strat_base

        # MWV and Surrender charge rate lookup
        surr_charge = cls.get_surrender_chrg(row, state, surr_chrg_table)
        mwv = cls.calculate_mwv(row, state, av, timing, strat_r, trading_cost, surr_charge)

        # Collect AS items
        locked_info = {"auto_locked": state.auto_locked, "manual_locked": state.manual_locked, "lock_credited": state.lock_credited} 
        AS_txn_amt = cls.calculate_AS_txn_amt(state)

        # Collect outputs
        out = {
            "": "",
            **trxn,
            "AS_txn_amt": AS_txn_amt,
            **timing,
            **t_info,
            "crediting_now": crediting_now,
            **param,
            "alt_fund_code": state.alt_code,
            **idx_info,
            "strategy_return": strat_r,
            **ouv,
            "trading_cost": trading_cost,
            "strategy_base": strat_base,
            **av,
            "sys_av_c": state.fund_bal,
            "diff_av": av["av_end_txn"] - state.fund_bal,
            **mwv,
            "sys_mwv_c": row.get("STRATEGY_MWV_" + state.fund_code),
            "diff_mwv": mwv["mwv_interim"] - row.get("STRATEGY_MWV_" + state.fund_code),
            "fund_gross_wdl": state.fund_gross_wd,
            "surr_charge": surr_charge,
            "fund_net_wdl": state.fund_net_wd,
            "pol_fwv": state.fwv,
            "fund_attribution": state.fund_portion,
            **locked_info, 
            "Remaining_Time_Pct": state.remaining_time_pct
        }
        # round to 2 digits if float in dict out
        out = {k: round(v, 2) if isinstance(v, float) else v for k, v in out.items()}
        return out