# ruff: noqa: F541
# ruff: noqa: F401
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from decimal import Decimal
import os

import numpy as np
import pandas as pd
from collections import OrderedDict
from PolicyTrxnDataJson import PolicyTrxnDataJson
import json
import logging
_logger = logging.getLogger(__name__)
# =========================
# Helpers
# =========================

INDEXVALUATION_TYPES = {"Index Valuation"}
RESET_TYPES = {"Deposit", "Reallocation Transfer"}  # reset 2nd anniversary / indexes etc.

def to_decimal(val: Any) -> Optional[Decimal]:
    if pd.isna(val):
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None

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

    # trxn type
    prev_txn_type: Optional[str] = None
    # timing
    second_anniv: Optional[date] = None
    is_terminated: bool = False
    prev_is_2nd_anniv: bool = False

    # index tracking
    index_name: Optional[str] = None
    prev_index_value: Optional[float] = None  # previous observed index level used for return calc
    last_index_value: Optional[float] = None  # last index value carried forward
    last_index_return: Optional[float] = None # last computed index return (decimal)
    prev_crediting_now: bool = False

    # strat base
    strat_base: Optional[float] = None  # strategy base

    # AV rollforward
    prev_av_eop_policy: float = 0.0 # Track last txn post everything AV
    av_end_txn: float = 0.0  # Post everything AV

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
    accum_prem: float = 0.0
    fwv: float = 0.0

    bop_date: Optional[date] = None
    eop_date: Optional[date] = None
    remaining_time_pct: Optional[float] = None
    pol_yr: int = 0
    deposit_ct: int = 0
    dt: float = 0.0

    mwv_rate: float = 0.0
    mwv: float = 0.0
    mwv_base: float = 0.0

    mva_index_bop: Optional[float] = None
    net_premium: float = 0.0

    # boundary context for calculate_timing
    last_pol: Optional[str] = None
    last_day: Optional[date] = None
    last_txn: Optional[str] = None
    last_fund: Optional[str] = None


class RollForward_policy:

    # -----------------------------
    # Main entry
    # -----------------------------
    # ---------- Top-level policy-level runner ----------
    @classmethod
    def get_rollforward_from_extract(cls,
                                     data_long: pd.DataFrame,
                                     results_basepath: str,
                                     surr_file_path: str,
                                     fund_mapping: dict,
                                     pols_dict) -> pd.DataFrame:
        """
        Policy-level runner:
          - Sort data_full_with_av for stability
          - For each row: compute policy-level rollforward w/ av_eop already provided
          - Save data_with_rollforward_policy_level.csv
        """
        os.makedirs(results_basepath, exist_ok=True)
        # unpack grouped dictionary from read excel sheet 
        # and make available for the entire class
        cls.FUND_INDEX_MAP = fund_mapping["FUND_INDEX_MAP"]
        cls.FUND_EQINDEX_MAP = fund_mapping["FUND_EQINDEX_MAP"]

        # surrender dict
        from RollForward_base import RollForward_base as _RB
        surr_dict = _RB.load_surr_chrg_table_to_dict(surr_file_path, results_basepath)

        # sort (policy → txn → date)
        # sort_cols = [c for c in ["POLICY_NUM", "TRANSACTION_NUM", "EFFECTIVE_DATE"] if c in data_long.columns]
        # df = data_long.sort_values(by=sort_cols, kind="mergesort").reset_index(drop=True)
        df = data_long

        # state per policy (no fund dimension here)
        states: Dict[str, GMDBState] = {}
        row_results: List[pd.Series] = []
        _logger.info(f"processing each transaction row per policy...")
        for _, row in df.iterrows():
            pol = str(row.get("POLICY_NUM", ""))
            state = states.get(pol)
            if state is None:
                state = GMDBState()
                states[pol] = state

            calc_per_policy = cls.process_row_per_policy(row, state, surr_dict)
            row_w_rollforward = pd.concat([row, pd.Series(calc_per_policy)], axis=0)
            row_results.append(row_w_rollforward)

        # save
        rollforward_results_policy = pd.DataFrame(row_results)
        out_path = os.path.join(results_basepath, "4. Data_Pol_Level_wRollforward.csv")
        rollforward_results_policy.to_csv(out_path, index=False)


        # append some policy level items to output JSON data struct
        for _, row in rollforward_results_policy.iterrows():
            pol_num = _to_str(row.get("POLICY_NUM"))
            cur_day = _to_str(row.get("EFFECTIVE_DATE"))
            cur_txn = _to_str(row.get("TRANSACTION_NUM"))   # Keep transaction number as *string* for stable grouping
            csv = row.get("csv")
            death_ben = row.get("death_ben")
            
            pols_dict = cls.add_pol_level_info(pols_dict, pol_num, cur_day, cur_txn, csv, death_ben)
            pols_dict = cls.add_txn_level_info(pols_dict, pol_num, cur_day, cur_txn)
        return pols_dict

    # -------------------------------------------------------------
    # 1) Transaction amounts (Fee / Withdrawal / accum_wd, priorWd)
    # -------------------------------------------------------------
    @staticmethod
    def get_trxn_amount(row: pd.Series, state: GMDBState) -> Dict[str, Any]:
        txn_type = str(row.get("TRANSACTION_DESC", "")).strip()
        fee = _safe_float(row.get("FEE")) if txn_type == "GLB/GMWB FEE" else 0.0
        wd = _safe_float(row.get("WITHDRAWAL")) if txn_type == "Withdrawal" else 0.0
        prem = _safe_float(row.get("PREMIUM")) if txn_type == "Initial Premium" else 0.0
        accum_prem = state.accum_prem + prem

        if txn_type in RESET_TYPES:
            accum_wd = 0.0
            is_withdrawal = False
            priorWd = 0.0
        else:
            accum_wd = 0.0 if state.prev_txn_type in INDEXVALUATION_TYPES else state.accum_wd + wd
            is_withdrawal = (txn_type == "Withdrawal") or state.is_withdrawal
            priorWd = state.priorWd
        state.accum_wd = accum_wd
        state.is_withdrawal = is_withdrawal
        state.priorWd = wd
        state.accum_prem = accum_prem
        return {"Fee": fee, "Wd": wd, "accum_wd": accum_wd, "has_withdrawal_taken": is_withdrawal,
                "priorWd": priorWd, "accum_prem": accum_prem}

    # -------------------------------------------------------------
    # 3) Timing (extended boundary + 2nd anniversary logic)
    # -------------------------------------------------------------
    @staticmethod
    def calculate_timing(row: pd.Series, state: GMDBState) -> Dict[str, Any]:
        """
        Revised rules with termination flag:
          - is_anniversary True only if TC_TXN_TYPE == 'Anniversary Valuation'.
          - 2nd_anniversary:
                * if type in {Deposit, Premium Bonus, ReAllocation}:
                    2nd_anniversary = TC_TXN_DATE + 2y
                * elif previous is_2nd_anniversary was True:
                    2nd_anniversary = previous 2nd_anniversary + 2y
                * elif is_anniversary is False:
                    carry previous 2nd_anniversary
                * else (is_anniversary True but previous is_2nd_anniversary False):
                    carry previous 2nd_anniversary
          - is_2nd_anniversary:
                False for reset rows;
                else (TC_TXN_DATE >= 2nd_anniversary)
          - is_terminated:
                True if previous txn_type == 'ReAllocation' and current TXN_AMT == 0
                Else carry previous value
        """
        txn_type = str(row.get("TRANSACTION_DESC", "")).strip()
        txn_date = _to_date(row.get("EFFECTIVE_DATE"))
        cur_pol = str(row.get("POLICY_NUM") or "")
        cur_day = txn_date
        cur_txn = str(row.get("TRANSACTION_NUM") or "")
        cur_fund = str(row.get("raw_fund_code") or "")

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
        is_new_fund = (cur_fund != state.last_fund) or is_new_policy

        # deposit flags + count
        is_deposit = (txn_type == "Deposit")
        has_first_deposit = is_new_policy and is_deposit
        has_second_deposit = (not is_new_policy) and is_deposit
        if has_first_deposit:
            state.deposit_ct = 1
        elif has_second_deposit:
            state.deposit_ct += 1
        deposit_ct = state.deposit_ct

        is_relocation = (txn_type == "Reallocation Transfer")
        # anniversary flags
        is_anniv = (txn_type in INDEXVALUATION_TYPES)
        reset = txn_type in RESET_TYPES
        prev_second = state.second_anniv
        prev_is_second = state.prev_is_2nd_anniv

        # evolve 2nd anniversary
        if reset:
            new_second = (txn_date + relativedelta(years=2)) if txn_date else prev_second
        elif prev_is_second:
            base = prev_second if prev_second else ((txn_date + relativedelta(years=2)) if txn_date else None)
            new_second = (base + relativedelta(years=2)) if base else None
        elif not is_anniv:
            new_second = prev_second
        else:
            new_second = prev_second

        if reset:
            is_second_flag = False
        else:
            is_second_flag = (txn_date is not None and new_second is not None and txn_date >= new_second)

        # # ---- termination flag ----
        # txn_amt = _safe_float(row.get("TXN_AMT")) or 0.0
        # if state.prev_txn_type == "ReAllocation" and txn_amt == 0:
        #     is_terminated = True
        # else:
        #     is_terminated = state.is_terminated  # carry forward


        # update boundary context
        state.last_pol = cur_pol
        state.last_day = cur_day
        state.last_txn = cur_txn
        state.last_fund = cur_fund

        state.second_anniv = new_second
        state.prev_is_2nd_anniv = is_second_flag

        # update termination state
        state.prev_txn_type = txn_type
        # state.is_terminated = is_terminated

        return {
            "is_new_policy": is_new_policy,
            "is_new_day": is_new_day,
            "is_new_transaction": is_new_transaction,
            "is_new_fund": is_new_fund,
            "has_first_deposit": has_first_deposit,
            "has_second_deposit": has_second_deposit,
            "deposit_ct": deposit_ct,
            "is_relocation": is_relocation,
            "is_anniversary": is_anniv,
            "2nd_anniversary": new_second,
            "is_2nd_anniversary": is_second_flag,
            # "is_terminated": is_terminated,
            # "is_active": is_active
        }

    # -------------------------------------------------------------
    # 4) t, dt, bop/eop, pol_yr
    # -------------------------------------------------------------
    @staticmethod
    def calculate_t(row: pd.Series, state: GMDBState, timing: Dict[str, Any]) -> Dict[str, Any]:
        eop_date = _to_date(row.get("EFFECTIVE_DATE"))
        bop_date = eop_date if timing["is_new_policy"] else (state.bop_date or eop_date)
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

        return {"t": t, "dt": dt, "bop_date": bop_date, "eop_date": eop_date, "pol_yr": pol_yr}

    # -------------------------------------------------------------
    # 5) Net Premium tracking
    # -------------------------------------------------------------
    @staticmethod
    def calculate_net_premium(row: pd.Series, state: GMDBState) -> Dict[str, Any]:
        
        if row.get("TRANSACTION_DESC") == "Initial Premium":
            net_premium = row.get("PREMIUM")
        # no txn amt then keep constant
        elif row.get("GrWd+PErecap") == 0 or pd.isna(row.get("GrWd+PErecap")) or row.get("TRANSACTION_DESC") == "ROP Death Benefit Fee":
            net_premium = state.net_premium
        # deduction on net premium prorated to change in interim value 
        else:
            net_premium = state.net_premium * row.get("av_end_txn") / (row.get("av_end_txn") - row.get("GrWd+PErecap"))
            
        state.net_premium = net_premium

        return net_premium


    # -------------------------------------------------------------
    # 6) ROP DB fee prorated at each txn date
    # -------------------------------------------------------------
    @staticmethod
    def calculate_fee(row: pd.Series, state: GMDBState, net_prem: float) -> Dict[str, Any]:
        # return 0 if not a ROP policy
        if row.get("DB_OPTION") != "Return of Premium":
            return 0

        issue_date = pd.to_datetime(state.bop_date)
        
        txn_date = pd.to_datetime(row.get("EFFECTIVE_DATE"))
        if txn_date != issue_date:
            start_of_pol_year = pd.Timestamp(year=txn_date.year, month=issue_date.month, day=issue_date.day)
        else:
            start_of_pol_year = issue_date
        end_of_pol_year =  start_of_pol_year + pd.DateOffset(years=1)
        elapsed_days_of_current_pol_year = txn_date - start_of_pol_year

        if row.get("TRANSACTION_DESC") in {"Index Valuation", "ROP Death Benefit Fee"}:
            elapsed_pct_of_current_pol_year = 1
        else:
            elapsed_pct_of_current_pol_year = elapsed_days_of_current_pol_year / (end_of_pol_year - start_of_pol_year)

        fee_rate = 0.002
        prorated_fee_amt = fee_rate * net_prem * elapsed_pct_of_current_pol_year


        return prorated_fee_amt

    
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
        prod = str(row.get("PL_PRD_DESC") or "").upper()  # <-- ensure uppercase to match dict keys
        prod_cd_idx = 0  # always 0 per spec

        try:
            return float(
                surr_dict[admin][st][prod][prod_cd_idx][pol_idx]
            )
        except Exception:
            return 0.0

    # -------------------------------------------------------------
    # 12) CSV (cash surrender value) + MVA
    # -------------------------------------------------------------
    @staticmethod
    def calculate_csv(av_eop: float, mwv: float, surr_rate: float, fee: float, net_prem: float, 
                      state: GMDBState, row: pd.Series) -> Dict[str, Any]:
        txn_type = row.get("TRANSACTION_DESC")
        gross_wdl_amt = row.get("GrWd+PErecap")

        # 1. fwv entitlement 10%
        fwv = av_eop * 0.10
        if txn_type in {"Initial Premium", "Annual Processing", "ROP Death Benefit Fee", "Reallocation Transfer"}:
            fwv = av_eop * 0.1
        # reduce fwv amount at withdrawals
        elif txn_type in {"Systematic Withdrawal", "Withdrawal", "Automatic Minimum Distribution", "72(t)/72(q) Withdrawal"}:
            fwv = max(0, state.fwv + gross_wdl_amt)
        else:
            fwv = state.fwv
        state.fwv = fwv
        # 2. surrender charge
        surr_chrg = max(0.0, (av_eop - fwv)) * (_safe_float(surr_rate) / 100.0)

        # 3. csv and death benefit
        csv_val = max(mwv, av_eop - surr_chrg - fee)
        death_ben = max(av_eop - fee, mwv, net_prem)

        return {
            "fwd_amt": fwv,
            "surr_chrg": surr_chrg,
            "csv": csv_val,
            "death_ben": death_ben
        }

    @staticmethod   
    def add_pol_level_info(pols_dict: Dict[str, Any], pol_num: str, cur_day: str, cur_txn: str, csv: float, death_ben: float) -> Dict[str, Any]:
        for _, pol in pols_dict.items():
            if pol.get("num") != pol_num:
                continue
            days = pol.get("days", {}) or {}
            for _, day in days.items():
                if day.get("date") != cur_day:
                    continue
                txns = day.get("transactions", {}) or {}
                for _, txn in txns.items():
                    if str(txn.get("transaction_num")) == cur_txn:
                        txn.update({"csv": str(csv), "death_benefit": str(death_ben)})

        return pols_dict

    @staticmethod   
    def add_txn_level_info(pols_dict: Dict[str, Any], pol_num: str, cur_day: str, cur_txn: str) -> Dict[str, Any]:
        """
        Adding aggregated fund level interest credited to the transaction level
        """
        for _, pol in pols_dict.items():
            if pol.get("num") != pol_num:
                continue
            days = pol.get("days", {}) or {}
            for _, day in days.items():
                if day.get("date") != cur_day:
                    continue
                txns = day.get("transactions", {}) or {}
                for _, txn in txns.items():
                    if str(txn.get("transaction_num")) == cur_txn:
                        funds = txn.get("funds", {}) or {}
                        txn_interest_credited = Decimal("0")
                        for fund in funds.values():
                            fund_interest_credited = fund.get("interest_credited") or ""
                            txn_interest_credited = txn_interest_credited + to_decimal(fund_interest_credited)
                        txn.update({"interest_credited": str(txn_interest_credited)})

        return pols_dict
    # ---------- MAIN per-row policy-level calc ----------
    @classmethod
    def process_row_per_policy(cls, row: pd.Series, state: GMDBState, surr_chrg_table_oasys: Dict[str, Any]) -> \
    Dict[str, Any]:
        # product flags
        trxn = cls.get_trxn_amount(row, state)

        # timing + boundaries
        timing = cls.calculate_timing(row, state)

        # t/dt/policy-year
        t_info = cls.calculate_t(row, state, timing)  # updates bop/eop/pol_yr

        net_prem = cls.calculate_net_premium(row, state)

        # ROP DB Fee if applicable
        fee = cls.calculate_fee(row, state, net_prem)

        # For downstream functions that expect an 'av' dict, prepare minimal stub
        av_eop_val = _safe_float(row.get("av_end_txn"))

        # MWV
        # Directly copy from 3rd csv output
        mwv = row.get("mwv_interim")

        # Surrender charge rate lookup + CSV
        surr_rate = cls.get_surrender_chrg(row, state, surr_chrg_table_oasys)
        csv_info = cls.calculate_csv(av_eop_val, mwv, surr_rate, fee, net_prem, state, row)
        
        out = {
            **trxn,
            **timing,
            **t_info,
            "mwv": mwv,
            "surr_rate": surr_rate,
            "Net_prem": net_prem,
            "ROP fee": fee,
            **csv_info
        }
        return out





