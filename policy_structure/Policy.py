from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date
from collections import OrderedDict
import pandas as pd
from decimal import Decimal

def _to_decimal(val) -> Decimal:
    """Convert any value to Decimal; returns Decimal('0') on failure."""
    if val is None:
        return Decimal("0")
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")

# Local lightweight helpers (kept here to avoid circular imports)
def to_date(val: Any) -> Optional[date]:
    if pd.isna(val):
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

def to_str(val: Any) -> Optional[str]:
    if pd.isna(val):
        return None
    return str(val)


def _format_percentage_str(val: Any) -> Optional[str]:
    """Convert Decimal or numeric value to percentage string like '00.00%'."""
    if val is None:
        return None
    try:
        num = float(val)
        return f"{num:06.2f}%"
    except (ValueError, TypeError):
        return None


@dataclass
class Policy:
    # Common attributes
    num: Optional[str] = None
    status: Optional[str] = None

    qualType: Optional[str] = None
    prdName: Optional[str] = None
    state: Optional[str] = None
    issDate: Optional[date] = None
    progElecDate: Optional[date] = None
    progEffDate: Optional[date] = None
    progAvailDate: Optional[date] = None


    days: "OrderedDict[int, Dict[str, Any]]" = field(default_factory=OrderedDict)

    # ====== WB (legacy) ======
    @classmethod
    def from_row(cls, row: pd.Series) -> "Policy":
        """Original WB mapping (unchanged)."""
        return cls(
            num=to_str(row.get("POLICY_NUM")),
            status=to_str(row.get("PC_STATUS")),
            qualType=to_str(row.get("QU_QUAL_DESC")),
            prdName=to_str(row.get("PL_PRD_DESC")),
            state=to_str(row.get("PC_CONT_STATE_CODE")),
            issDate=to_date(row.get("ISSUED_DATE")),
            # overrideDate=to_date(row.get("PO_GMWB_OVERRIDE_DATE")),
            # sysElecDate=to_date(row.get("PO_GMWB_ELECTION_DATE")),
            # sysEffDate=to_date(row.get("PO_GMWB_EFF_DATE")),
            # sysAvailDate=to_date(row.get("PO_GMWB_AVAIL_DATE")),

            # placeholders
            progElecDate=to_date(row.get("PC_POL_DATE")),
            progEffDate=to_date(row.get("PC_POL_DATE")),
            progAvailDate=to_date(row.get("PC_POL_DATE")),

            # rollforward fields
            # is_gmwb=row.get("is_gmwb"),
            # is_multiplier=row.get("is_multiplier"),
        )

    def update_from_row(self, row: pd.Series) -> None:
        """Original WB updater (unchanged)."""
        self.num = self.num or to_str(row.get("POLICY_NUM"))
        self.status = self.status or to_str(row.get("PC_STATUS"))

        self.qualType = self.qualType or to_str(row.get("QU_QUAL_DESC"))
        self.prdName = self.prdName or to_str(row.get("PL_PRD_DESC"))
        self.state = self.state or to_str(row.get("PC_CONT_STATE_CODE"))
        self.issDate = self.issDate or to_date(row.get("ISSUED_DATE"))
        self.progElecDate = self.progElecDate or to_date(row.get("PROG_ELECTION_DATE"))
        self.progEffDate = self.progEffDate or to_date(row.get("PROG_EFF_DATE"))
        self.is_gmwbprogAvailDate = self.progAvailDate or to_date(row.get("PROG_AVAIL_DATE"))

        # self.is_gmwb = self.is_gmwb or row.get("is_gmwb")
        # self.is_multiplier = self.is_multiplier or row.get("is_multiplier")


    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for key, val in list(d.items()):
            if isinstance(val, (date, datetime)):
                d[key] = val.isoformat()
        d["days"] = dict(self.days)
        return d

    @classmethod
    def _find_last_lock(cls, policy_dict: Dict[str, Any], fund_code: str) -> Tuple[Optional[Decimal], Optional[str], Optional[str]]:
        """
        Return (fund_av_at_lock, lock_day_key, lock_txn_key) from the "Lock Threshold Achieved"
        transaction that contains the given fund_code. At most one such txn exists per fund.
        Returns (None, None, None) if not found.
        """
        days = policy_dict.get("days", {}) or {}
        for day_key, day_data in days.items():
            txns = day_data.get("transactions", {}) or {}
            for txn_key, txn in txns.items():
                if txn.get("transaction_type") == "Lock Threshold Achieved":
                    funds = txn.get("funds", {}) or {}
                    for fund_dict in funds.values():
                        if fund_dict.get("raw_fund_code") == fund_code:
                            return _to_decimal(fund_dict.get("av")), day_key, txn_key
        return None, None, None

    @classmethod
    def _sum_interest_post_lock(cls, policy_dict: Dict[str, Any], fund_code: str, lock_day_key: str, lock_txn_key: str) -> Decimal:
        """
        Sum the interest credited amount for a given fund across all transactions
        that occur after the lock transaction (by day/txn order).
        """
        days = policy_dict.get("days", {}) or {}
        day_keys = sorted(days.keys(), key=lambda k: days[k].get("date", ""))

        total = Decimal("0")
        in_range = False

        for day_key in day_keys:
            day_data = days[day_key]
            txns = day_data.get("transactions", {}) or {}
            txn_keys = list(txns.keys())

            for txn_key in txn_keys:
                if day_key == lock_day_key and txn_key == lock_txn_key:
                    in_range = True
                    continue
                if not in_range:
                    continue

                txn = txns[txn_key]
                funds = txn.get("funds", {}) or {}
                for fund_dict in funds.values():
                    if fund_dict.get("raw_fund_code") == fund_code:
                        ic_amt = _to_decimal(fund_dict.get("interest_credited"))
                        total += ic_amt
                        break

        return total

    @classmethod
    def _find_all_anniversaries(cls, policy_dict: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str, str]]:
        """
        Return list of (txn_dict, day_key, txn_key) for all anniversary transactions,
        sorted by date ascending.
        """
        anniversary_types = {"Initial Premium", "Annual Processing"}
        days = policy_dict.get("days", {}) or {}
        results = []

        for day_key, day_data in days.items():
            txns = day_data.get("transactions", {}) or {}
            for txn_key, txn in txns.items():
                if txn.get("transaction_type") in anniversary_types:
                    results.append((txn, day_key, txn_key))

        # Sort by date, then by txn_key for same-day ordering
        results.sort(key=lambda x: (x[0].get("date", ""), x[2]))
        return results

    @classmethod
    def _find_last_anniversary(cls, policy_dict: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Return the latest anniversary transaction (by date), not just last by index."""
        anniversaries = cls._find_all_anniversaries(policy_dict)
        if not anniversaries:
            return None, None
        last = anniversaries[-1]
        return last[0], last[1]

    @classmethod
    def _find_prev_anniversary(cls, policy_dict: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Return the second-to-last anniversary transaction (previous anniversary)."""
        anniversaries = cls._find_all_anniversaries(policy_dict)
        if len(anniversaries) < 2:
            return None, None
        prev = anniversaries[-2]
        return prev[0], prev[1]

    @classmethod
    def _find_issue_txn(cls, policy_dict: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Return the 'Initial Premium' transaction with the largest transaction_num
        among those on the earliest issue date. Returns (txn_dict, day_key).
        """
        days = policy_dict.get("days", {}) or {}
        results: List[Tuple[Dict[str, Any], str, str]] = []

        for day_key, day_data in days.items():
            txns = day_data.get("transactions", {}) or {}
            for txn_key, txn in txns.items():
                if txn.get("transaction_type") == "Initial Premium":
                    results.append((txn, day_key, txn_key))

        if not results:
            return None, None

        # Sort by date ascending, then by transaction_num descending (as string for safety)
        results.sort(key=lambda x: (x[0].get("date", ""), x[0].get("transaction_num", "")), reverse=False)
        # Re-sort just the same-date group by txn_num descending
        first_date = results[0][0].get("date", "")
        same_date = [r for r in results if r[0].get("date") == first_date]
        same_date.sort(key=lambda x: x[0].get("transaction_num", ""), reverse=True)
        best = same_date[0]
        return best[0], best[1]

    @classmethod
    def _get_day_date(cls, policy_dict: Dict[str, Any], day_key: str) -> Optional[str]:
        """Get the date string for a given day key."""
        days = policy_dict.get("days", {}) or {}
        day_data = days.get(day_key, {})
        return day_data.get("date")

    @classmethod
    def _sum_txn_amounts_between_anniversaries(
        cls, policy_dict: Dict[str, Any], start_day_key: Optional[str], end_day_key: Optional[str]
    ) -> Dict[str, Decimal]:
        """
        Sum fund-level 'prem' values for transactions strictly between start_day and end_day
        (exclusive of start, inclusive of end). If start_day is None, sums from the beginning.
        """
        sums = {
            "premium": Decimal("0"),
            "withdrawal": Decimal("0"),
            "fee": Decimal("0"),
            "interest": Decimal("0"),
        }

        days = policy_dict.get("days", {}) or {}
        day_keys = sorted(days.keys(), key=lambda k: days[k].get("date", ""))

        in_range = start_day_key is None  # If None, include all from start
        for day_key in day_keys:
            if start_day_key is not None and day_key == start_day_key:
                in_range = True
                # Don't skip; fall through to include this day
            if not in_range:
                continue

            day_data = days[day_key]
            txns = day_data.get("transactions", {}) or {}
            for txn in txns.values():
                txn_type = txn.get("transaction_type", "")
                funds = txn.get("funds", {}) or {}

                if txn_type == "Initial Premium":
                    # Sum fund-level 'prem' for premium transactions
                    txn_prem = Decimal("0")
                    for fund in funds.values():
                        prem_val = fund.get("prem")
                        if prem_val is not None:
                            try:
                                txn_prem += Decimal(str(prem_val))
                            except Exception:
                                pass
                    sums["premium"] += txn_prem
                elif txn_type == "ROP Death Benefit Fee":
                    # Use fund-level txn_amt for ROP Death Benefit Fee
                    for fund in funds.values():
                        fund_amt = fund.get("txn_amt")
                        if fund_amt is not None:
                            try:
                                sums["withdrawal"] += Decimal(str(fund_amt))
                            except Exception:
                                pass
                elif txn_type == "Withdrawal":
                    # Use transaction-level txn_amount for withdrawals
                    amt_str = txn.get("txn_amount", "0")
                    try:
                        sums["withdrawal"] += Decimal(str(amt_str))
                    except Exception:
                        pass

                interest_credited = Decimal(txn.get("interest_credited", "0"))
                sums["interest"] += interest_credited

            if day_key == end_day_key:
                break

        return sums

    @classmethod
    def extract_pol_level_items(cls, policy_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract Annual Statement items for a policy from pols_structure.json format.

        Returns a dict with:
            - contract_value_prev_anniversary: AV/MWV as of previous anniversary
            - contract_value_curr_anniversary: AV/MWV as of current anniversary
            - premium_since_issue: cumulative premium since issue
            - premium_this_period: premium this statement period (between prev and curr anniversary)
            - withdrawals_since_issue: cumulative withdrawals since issue
            - withdrawals_this_period: withdrawals this statement period
            - interest_credited_since_issue: interest credited since issue
            - interest_credited_this_period: interest credited this statement period
        """
        from .Transaction import Transaction

        # Find key anniversaries
        curr_ann_txn, curr_day_key = cls._find_last_anniversary(policy_dict)
        prev_ann_txn, prev_day_key = cls._find_prev_anniversary(policy_dict)
        issue_txn, _ = cls._find_issue_txn(policy_dict)

        # Pol level csv and DB
        csv = curr_ann_txn.get("csv")
        death_benefit = curr_ann_txn.get("death_benefit")

        # Aggregate fund values for current anniversary
        curr_values = {}
        if curr_ann_txn:
            curr_values = Transaction.aggregate_fund_values_from_dict(curr_ann_txn)

        # Aggregate fund values for previous anniversary
        prev_values = {}
        if prev_ann_txn:
            prev_values = Transaction.aggregate_fund_values_from_dict(prev_ann_txn)

        # Aggregate fund values for issue transaction
        issue_values = {}
        if issue_txn:
            issue_values = Transaction.aggregate_fund_values_from_dict(issue_txn)

        # Sum premiums and withdrawals since issue (all transactions)
        since_issue = cls._sum_txn_amounts_between_anniversaries(policy_dict, None, curr_day_key)

        # Sum premiums and withdrawals this period (between prev and curr anniversary)
        this_period = cls._sum_txn_amounts_between_anniversaries(policy_dict, prev_day_key, curr_day_key)

        cmv_since_issue = (curr_values.get("total_av", Decimal("0")) - since_issue["premium"] - since_issue["interest"] - 
                           since_issue["withdrawal"] - issue_values.get("total_av", Decimal("0")))
        cmv_this_period = (curr_values.get("total_av", Decimal("0")) - this_period["premium"] - this_period["interest"] - 
                           this_period["withdrawal"] - prev_values.get("total_av", Decimal("0")))
        return {
            "av_prev_ann_contsummary": prev_values.get("total_av", Decimal("0")),
            "av_curr_ann_contsummary": curr_values.get("total_av", Decimal("0")),
            "prem_since_issue_contsummary": since_issue["premium"],
            "prem_this_period_contsummary": this_period["premium"],
            "withdrawals_since_issue": since_issue["withdrawal"],
            "withdrawals_this_period": this_period["withdrawal"],
            "interest_since_issue": since_issue["interest"],
            "interest_this_period": this_period["interest"],
            "cmv_since_issue": cmv_since_issue,
            "cmv_this_period": cmv_this_period,
            "account_value_impinfo": curr_values.get("total_av", Decimal("0")),
            "minwdl_value_impinfo": curr_values.get("total_mwv", Decimal("0")),
            "cash_value_impinfo": csv, 
            "death_benefit_impinfo": death_benefit,
        }

    @classmethod
    def extract_fund_level_items(cls, policy_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract fund-level Annual Statement items for a policy from pols_structure.json.

        For each fund in the latest anniversary transaction, returns:
            - fund_code, fund_name
            - term_start_date, term_end_date
            - contract_value_curr_ann av
            - alloc_pct, buffer, upside_param_type, upside_param_rate
            - beg_equity_index_value, end_equity_index_value
            - raw_index_change_pct (calculated)
            - strategy_return (sourced from fund data)
        """
        # Find specific anniversary transactions: prioritize "ROP Death Benefit Fee", fallback to "Index Valuation"
        days = policy_dict.get("days", {}) or {}
        rop_results = []
        idx_results = []

        for day_key, day_data in days.items():
            txns = day_data.get("transactions", {}) or {}
            for txn_key, txn in txns.items():
                t_type = txn.get("transaction_type")
                if t_type == "ROP Death Benefit Fee":
                    rop_results.append((txn, day_key, txn_key))
                elif t_type == "Index Valuation":
                    idx_results.append((txn, day_key, txn_key))

        # Sort by date ascending to find the latest
        sort_key = lambda x: (x[0].get("date", ""), x[2])  # noqa: E731
        rop_results.sort(key=sort_key)
        idx_results.sort(key=sort_key)

        # Prioritize ROP if available, otherwise Index Valuation
        if rop_results:
            curr_ann_txn, _, _ = rop_results[-1]
        elif idx_results:
            curr_ann_txn, _, _ = idx_results[-1]
        else:
            curr_ann_txn = None

        if not curr_ann_txn:
            return []

        policy_num = policy_dict.get("num")
        curr_ann_date = curr_ann_txn.get("date")
        curr_funds = curr_ann_txn.get("funds", {}) or {}

        eligible_lock_codes = {"RO", "RN", "RM", "RP", "RR", "RQ"}

        # Find latest anniversary transaction for term dates
        latest_ann_txn, _ = cls._find_last_anniversary(policy_dict)
        ann_funds_map = {}
        if latest_ann_txn:
            ann_funds = latest_ann_txn.get("funds", {}) or {}
            for fund_dict in ann_funds.values():
                fund_code = fund_dict.get("raw_fund_code") or ""
                ann_funds_map[fund_code] = fund_dict

        # Find latest Index Valuation transaction for EOT items
        idx_valuation_txn = None
        if idx_results:
            idx_valuation_txn, _, _ = idx_results[-1]

        # Build a map of funds from the latest Index Valuation transaction (keyed by fund code)
        idx_funds_map = {}
        if idx_valuation_txn:
            idx_funds = idx_valuation_txn.get("funds", {}) or {}
            for fund_dict in idx_funds.values():
                fund_code = fund_dict.get("raw_fund_code") or ""
                idx_funds_map[fund_code] = fund_dict

        # Find latest ROP Death Benefit Fee transaction for eot_account_value
        rop_valuation_txn = None
        if rop_results:
            rop_valuation_txn, _, _ = rop_results[-1]

        # Calculate total allocation value across all funds for percentage calculation
        total_alloc_value = Decimal("0")
        for fund in curr_funds.values():
            total_alloc_value += _to_decimal(fund.get("av"))

        results = []
        for fund_dict in curr_funds.values():
            code = fund_dict.get("raw_fund_code") or ""
            name = fund_dict.get("fund_name") or ""

            av = _to_decimal(fund_dict.get("av"))
            strat_base = _to_decimal(fund_dict.get("strat_base"))
            index_credited_pct = Decimal("0")

            # --- calculated fields ---
            beg_idx = _to_decimal(fund_dict.get("beg_equity_index_value"))
            end_idx = _to_decimal(fund_dict.get("end_equity_index_value"))
            raw_index_change_pct = cls._pct_change(beg_idx, end_idx)
            

            # Determine fund type
            term_end = fund_dict.get("term_end_date")
            is_locked = fund_dict.get("auto_locked") is True or fund_dict.get("manual_locked") is True
            is_mid_term = term_end is not None and curr_ann_date is not None and term_end > curr_ann_date
            is_end_of_term = term_end is not None and curr_ann_date is not None and term_end == curr_ann_date

            # Lock threshold AV + interest credited post-lock
            if code in eligible_lock_codes:
                contract_value_locked_av, lock_day_key, lock_txn_key = cls._find_last_lock(policy_dict, code)
                interest_credited_post_lock = (
                    cls._sum_interest_post_lock(policy_dict, code, lock_day_key, lock_txn_key)
                    if lock_day_key is not None else Decimal("0")
                )
            else:
                contract_value_locked_av = None
                interest_credited_post_lock = None

            # Conditionally populate locked fund fields
            if is_locked and contract_value_locked_av is not None:
                locked_fund_name = name
                av_locked = contract_value_locked_av
                lock_fixed_rate = Decimal("0.01")
                int_credited_fixed = interest_credited_post_lock
                av_locked_plus_int = contract_value_locked_av + interest_credited_post_lock
            else:
                locked_fund_name = None
                av_locked = None
                lock_fixed_rate = None
                int_credited_fixed = None
                av_locked_plus_int = None

            # Conditionally populate mid-term fund fields
            if is_mid_term:
                mid_fund_name = name
                mid_strat_base = strat_base
                mid_av = av
            else:
                mid_fund_name = None
                mid_strat_base = None
                mid_av = None

            # Conditionally populate end-of-term fund fields (from latest Index Valuation transaction)
            if is_end_of_term and code in idx_funds_map:
                idx_fund_dict = idx_funds_map[code]
                eot_fund_name = idx_fund_dict.get("fund_name")
                eot_strat_base = _to_decimal(idx_fund_dict.get("strat_base"))
                eot_index_credited = _to_decimal(idx_fund_dict.get("strategy_return")) * Decimal("100") if idx_fund_dict.get("strategy_return") is not None else None
                index_credited_pct = _to_decimal(idx_fund_dict.get("strategy_return")) * Decimal("100") if idx_fund_dict.get("strategy_return") is not None else None
                eot_interest_credited = _to_decimal(idx_fund_dict.get("interest_credited"))
                # Prioritize ROP for eot_account_value, fallback to Index Valuation
                eot_account_value = _to_decimal(idx_fund_dict.get("av"))
                if rop_valuation_txn:
                    rop_funds = rop_valuation_txn.get("funds", {}) or {}
                    for rop_fund in rop_funds.values():
                        if rop_fund.get("raw_fund_code") == code:
                            eot_account_value = _to_decimal(rop_fund.get("av"))
                            break
            else:
                eot_fund_name = None
                eot_strat_base = None
                eot_index_credited = None
                eot_interest_credited = None
                eot_account_value = None

            # assembling for individual AS items
            # Get term dates from latest anniversary transaction if available
            ann_fund = ann_funds_map.get(code)
            alloc_term_start = ann_fund.get("term_start_date") if ann_fund else None
            alloc_term_end = ann_fund.get("term_end_date") if ann_fund else None
            
            results.append({
                "policy_num": policy_num,
                # Allocation Details (always present)
                "alloc_fund_name": name,
                "alloc_term_start": alloc_term_start,
                "alloc_term_end": alloc_term_end,
                "alloc_contract_value": av,
                "alloc_pct": round((av / total_alloc_value * Decimal("100")), 2) if total_alloc_value > Decimal("0") else Decimal("0"),
                "alloc_buffer": fund_dict.get("buffer"),
                "alloc_upside_type": fund_dict.get("upside_param_type"),
                "alloc_upside_rate": fund_dict.get("upside_param_rate"),
                # Contract Value - Locked Funds
                "locked_fund_name": locked_fund_name,
                "av_locked": av_locked,
                "lock_fixed_rate": lock_fixed_rate,
                "int_credited_fixed": int_credited_fixed,
                "av_locked_plus_int": av_locked_plus_int,
                # Contract Value - Mid Term Funds
                "mid_fund_name": mid_fund_name,
                "mid_strat_base": mid_strat_base,
                "mid_av": mid_av,
                # Contract Value - End of Term Funds
                "eot_fund_name": eot_fund_name,
                "eot_strat_base": eot_strat_base,
                "eot_index_credited": eot_index_credited,
                "eot_interest_credited": eot_interest_credited,
                "eot_account_value": eot_account_value,
                # Performance Summary (always present)
                "perf_fund_name": name,
                "perf_term_start": fund_dict.get("term_start_date"),
                "perf_term_end": fund_dict.get("term_end_date"),
                "perf_beg_index_val": beg_idx,
                "perf_end_index_val": end_idx,
                "perf_raw_index_chg": _format_percentage_str(raw_index_change_pct),
                "perf_upside_rate": _format_percentage_str(fund_dict.get("upside_param_rate")),
                "perf_downside_buffer": _format_percentage_str(fund_dict.get("buffer")),
                "perf_index_credited": _format_percentage_str(index_credited_pct),
            })

        results.sort(key=lambda x: (x.get("policy_num") or "", x.get("alloc_fund_name") or ""))
        return results

    @staticmethod
    def _pct_change(old_val: Decimal, new_val: Decimal) -> Optional[Decimal]:
        """Calculate percentage change. Returns None if old_val is zero."""
        if old_val == Decimal("0"):
            return None
        try:
            return ((new_val - old_val) / old_val) * Decimal("100")
        except Exception:
            return None

    @classmethod
    def extract_txn_fund_level_items(cls, policy_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract transaction-level fund items for a policy.

        For each transaction with type code in {11, 18, 44, 90, 170, 71, 91, 92, 93},
        and for each fund within that transaction, returns:
            - policy_num, txn_date, txn_type_desc
            - fund_name, term_start_date, term_end_date, txn_amt (fund-level)
        
        For reallocation transactions (code 71):
            - First, identifies funds from the previous transaction where term_end_date 
              equals the current reallocation date and reallocates them out entirely
            - Then, processes funds being allocated into new positions (prem field)
        
        Results are sorted by policy_num, then by fund_name.
        """
        EXTRACT_TXN_CODES = ("11", "18", "44", "90", "170", "71", "91", "92", "93")

        policy_num = policy_dict.get("num")
        days = policy_dict.get("days", {}) or {}
        results = []
        
        # Build chronological list of all transactions to enable look-back
        all_txns = []
        for _, day_data in days.items():
            txns = day_data.get("transactions", {}) or {}
            for txn_key, txn in txns.items():
                all_txns.append((txn, txn_key))
        
        # Sort by transaction date for chronological processing
        all_txns.sort(key=lambda x: (x[0].get("date", ""), x[1]))

        for idx, (txn, _) in enumerate(all_txns):
            txn_code = str(txn.get("transaction_code"))  # Convert to string for comparison
            if txn_code not in EXTRACT_TXN_CODES:
                continue

            txn_date = txn.get("date")
            txn_type_desc = txn.get("transaction_type")

            funds = txn.get("funds", {}) or {}
            if txn_code == "71":  # Realloc handling
                # First, look at previous transaction to identify eligible reallocations
                if idx > 0:
                    prev_txn, _ = all_txns[idx - 1]
                    prev_txn_funds = prev_txn.get("funds", {}) or {}
                    
                    # Identify funds in previous transaction with term_end_date == current reallocation date
                    for prev_fund_dict in prev_txn_funds.values():
                        if prev_fund_dict.get("term_end_date") == txn_date:
                            # This fund is entirely reallocated out
                            fund_av = _to_decimal(prev_fund_dict.get("av"))
                            results.append({
                                "policy_num": policy_num,
                                "txn_date": txn_date,
                                "txn_type_desc": txn_type_desc,
                                "fund_name": prev_fund_dict.get("fund_name"),
                                "term_start_date": prev_fund_dict.get("term_start_date"),
                                "term_end_date": prev_fund_dict.get("term_end_date"),
                                "txn_amt": -fund_av,
                            })
                
                # Second, process funds being allocated into new positions
                for fund_dict in funds.values():
                    prem_val = _to_decimal(fund_dict.get("prem"))
                    if prem_val and prem_val > 0:
                        results.append({
                            "policy_num": policy_num,
                            "txn_date": txn_date,
                            "txn_type_desc": txn_type_desc,
                            "fund_name": fund_dict.get("fund_name"),
                            "term_start_date": fund_dict.get("term_start_date"),
                            "term_end_date": fund_dict.get("term_end_date"),
                            "txn_amt": prem_val,
                        })
            else:
                # For non-reallocation transactions, process each fund
                for fund_dict in funds.values():
                    results.append({
                        "policy_num": policy_num,
                        "txn_date": txn_date,
                        "txn_type_desc": txn_type_desc,
                        "fund_name": fund_dict.get("fund_name"),
                        "term_start_date": fund_dict.get("term_start_date"),
                        "term_end_date": fund_dict.get("term_end_date"),
                        "txn_amt": _to_decimal(fund_dict.get("txn_amt")),
                    })

        # Sort by policy_num, then by txn_type_desc, then by term_start_date, then by fund_name
        results.sort(key=lambda x: (x.get("policy_num") or "", x.get("txn_type_desc") or "", x.get("term_start_date") or "", x.get("fund_name") or ""))
        return results