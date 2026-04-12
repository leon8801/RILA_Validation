# ruff: noqa: E702
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple
from collections import OrderedDict
from datetime import datetime, date
import pandas as pd

# Domain classes
from policy_structure.Transaction import Transaction
from policy_structure.Day import Day
from policy_structure.Policy import Policy


class PolicyTrxnDataJson:

    # ---------------------------
    # Utility helpers (same style)
    # ---------------------------
    @staticmethod
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

    @staticmethod
    def to_str(val: Any) -> Optional[str]:
        if pd.isna(val):
            return None
        return str(val)

    @staticmethod
    def to_int(val: Any) -> Optional[int]:
        if pd.isna(val):
            return None
        try:
            return int(val)
        except Exception:
            return None

    # ----------------------------------------
    # Builder (DB): ingest ONE row, preserve original style/logic
    # ----------------------------------------
    @classmethod
    def build_policies_structure_db(
        cls,
        pols: "OrderedDict[int, Dict[str, Any]]",
        row: pd.Series,
        prev_pol: Optional[str],
        prev_day: Optional[date],
        prev_txn: Optional[Any],
        pol_idx: int,
        day_idx: int,
        txn_idx: int,
    ) -> Tuple[
        "OrderedDict[int, Dict[str, Any]]",
        Optional[str],
        Optional[date],
        Optional[Any],
        int,
        int,
        int,
        bool,
        bool,
        bool,
    ]:
        """
        Build (or update) the nested dictionary *for a single row*, following the
        original boundary rules and structure. Returns the updated dict and
        trackers/indices so the caller can maintain state across rows.

        Return tuple:
          (pols, prev_pol, prev_day, prev_txn, pol_idx, day_idx, txn_idx,
           newPol, newDay, newTransaction)
        """

        # Read current keys
        cur_pol = cls.to_str(row.get("POLICY_NUM"))
        cur_day = cls.to_date(row.get("EFFECTIVE_DATE"))

        # IMPORTANT: if TC_TXN_NUM is string in long data, keep it as stable key;
        # try int first; else keep string (faithful to original approach)
        raw_txn_num = row.get("EFFECTIVE_DATE")
        try:
            cur_txn = int(raw_txn_num)
        except Exception:
            cur_txn = cls.to_str(raw_txn_num)

        # Determine boundaries
        if cur_pol != prev_pol:
            newPol = True; newDay = True; newTransaction = True
        elif cur_day != prev_day:
            newPol = False; newDay = True; newTransaction = True
        elif cur_txn != prev_txn:
            newPol = False; newDay = False; newTransaction = True
        else:
            newPol = newDay = newTransaction = False

        # -- Handle new policy / update policy
        if newPol:
            pol_idx += 1
            day_idx = -1
            txn_idx = -1

            current_policy = Policy.from_row(row)
            pols[pol_idx] = current_policy.to_dict()
        else:
            current_policy_obj = Policy(**pols[pol_idx])
            current_policy_obj.update_from_row(row)
            pols[pol_idx] = current_policy_obj.to_dict()

        # -- Ensure days dict exists
        if "days" not in pols[pol_idx]:
            pols[pol_idx]["days"] = OrderedDict()

        # -- Handle new day / update day
        if newDay:
            day_idx += 1
            txn_idx = -1

            current_day = Day.from_row(row)
            pols[pol_idx]["days"][day_idx] = current_day.to_dict()
        else:
            current_day_obj = Day(**pols[pol_idx]["days"][day_idx])
            current_day_obj.update_from_row(row)
            pols[pol_idx]["days"][day_idx] = current_day_obj.to_dict()

        # -- Ensure transactions dict exists
        if "transactions" not in pols[pol_idx]["days"][day_idx]:
            pols[pol_idx]["days"][day_idx]["transactions"] = OrderedDict()

        # -- Handle new transaction / update transaction
        if newTransaction:
            txn_idx += 1
            current_txn = Transaction.from_row(row)
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = current_txn.to_dict()
        else:
            current_txn_obj = Transaction(**pols[pol_idx]["days"][day_idx]["transactions"][txn_idx])
            current_txn_obj.update_from_row(row)
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = current_txn_obj.to_dict()

        # -- Ensure funds dict exists at txn level
        if "funds" not in pols[pol_idx]["days"][day_idx]["transactions"][txn_idx]:
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx]["funds"] = OrderedDict()

        # -- Upsert fund (unique raw_fund_code per txn), provider aligned with original "roles" pattern
        def new_fund_idx_provider_factory(existing_funds: "OrderedDict[int, Dict[str, Any]]"):
            # local counter seeded by current size; increment on each call
            next_idx = max(existing_funds.keys()) + 1 if existing_funds else 0
            def _next():
                nonlocal next_idx
                val = next_idx
                next_idx += 1
                return val
            return _next

        # rehydrate txn for upsert_fund
        txn_obj = Transaction(**pols[pol_idx]["days"][day_idx]["transactions"][txn_idx])
        fund_idx_provider = new_fund_idx_provider_factory(txn_obj.funds)
        txn_obj.upsert_fund(row, fund_idx_provider)
        pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = txn_obj.to_dict()

        # -- Advance previous trackers (exactly as original)
        prev_pol, prev_day, prev_txn = cur_pol, cur_day, cur_txn

        return (pols, prev_pol, prev_day, prev_txn, pol_idx, day_idx, txn_idx, newPol, newDay, newTransaction)

    @classmethod
    def build_policies_structure(
            cls,
            pols: "OrderedDict[int, Dict[str, Any]]",
            row: pd.Series,
            newPol: Optional[bool],
            newDay: Optional[bool],
            newTransaction: Optional[bool],
            pol_idx: int,
            day_idx: int,
            txn_idx: int,
    ) -> Tuple[
        "OrderedDict[int, Dict[str, Any]]",
        int,
        int,
        int,
        bool,
        bool,
        bool,
    ]:
        """
        Build (or update) the nested dictionary *for a single row*, following the
        original boundary rules and structure. Returns the updated dict and
        trackers/indices so the caller can maintain state across rows.

        Return tuple:
          (pols, prev_pol, prev_day, prev_txn, pol_idx, day_idx, txn_idx,
           newPol, newDay, newTransaction)
        """

        # -- Handle new policy / update policy
        if newPol:
            pol_idx += 1
            day_idx = -1
            txn_idx = -1

            current_policy = Policy.from_row(row)
            pols[pol_idx] = current_policy.to_dict()
        else:
            current_policy_obj = Policy(**pols[pol_idx])
            current_policy_obj.update_from_row(row)
            pols[pol_idx] = current_policy_obj.to_dict()

        # -- Ensure days dict exists
        if "days" not in pols[pol_idx]:
            pols[pol_idx]["days"] = OrderedDict()

        # -- Handle new day / update day
        if newDay:
            day_idx += 1
            txn_idx = -1

            current_day = Day.from_row(row)
            pols[pol_idx]["days"][day_idx] = current_day.to_dict()
        else:
            current_day_obj = Day(**pols[pol_idx]["days"][day_idx])
            current_day_obj.update_from_row(row)
            pols[pol_idx]["days"][day_idx] = current_day_obj.to_dict()

        # -- Ensure transactions dict exists
        if "transactions" not in pols[pol_idx]["days"][day_idx]:
            pols[pol_idx]["days"][day_idx]["transactions"] = OrderedDict()

        # -- Handle new transaction / update transaction
        if newTransaction:
            txn_idx += 1
            current_txn = Transaction.from_row(row)
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = current_txn.to_dict()
        else:
            current_txn_obj = Transaction(**pols[pol_idx]["days"][day_idx]["transactions"][txn_idx])
            current_txn_obj.update_from_row(row)
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = current_txn_obj.to_dict()

        # -- Ensure funds dict exists at txn level
        if "funds" not in pols[pol_idx]["days"][day_idx]["transactions"][txn_idx]:
            pols[pol_idx]["days"][day_idx]["transactions"][txn_idx]["funds"] = OrderedDict()

        # -- Upsert fund (unique raw_fund_code per txn), provider aligned with original "roles" pattern
        def new_fund_idx_provider_factory(existing_funds: "OrderedDict[int, Dict[str, Any]]"):
            # local counter seeded by current size; increment on each call
            next_idx = max(existing_funds.keys()) + 1 if existing_funds else 0

            def _next():
                nonlocal next_idx
                val = next_idx
                next_idx += 1
                return val

            return _next

        # rehydrate txn for upsert_fund
        txn_obj = Transaction(**pols[pol_idx]["days"][day_idx]["transactions"][txn_idx])
        fund_idx_provider = new_fund_idx_provider_factory(txn_obj.funds)
        txn_obj.upsert_fund(row, fund_idx_provider)
        pols[pol_idx]["days"][day_idx]["transactions"][txn_idx] = txn_obj.to_dict()

        # # -- Advance previous trackers (exactly as original)
        # prev_pol, prev_day, prev_txn = cur_pol, cur_day, cur_txn

        return (pols, pol_idx, day_idx, txn_idx, newPol, newDay, newTransaction)
    # ----------------------------------------
    # Update-only over a whole DataFrame (no inserts)
    # ----------------------------------------
    @classmethod
    def update_policies_structure(
        cls,
        pols: "OrderedDict[int, Dict[str, Any]]",
        df: pd.DataFrame
    ) -> "OrderedDict[int, Dict[str, Any]]":
        """
        Update an existing nested dict with values from df (NO insertions).
        Only invokes update_from_row_* for existing Policy/Day/Transaction/Fund paths.
        """
        for _, row in df.iterrows():
            cur_pol = cls.to_str(row.get("PC_CONT"))
            cur_day = cls.to_date(row.get("TC_TXN_DATE"))

            raw_txn_num = row.get("TC_TXN_NUM")
            try:
                cur_txn = int(raw_txn_num)
            except Exception:
                cur_txn = cls.to_str(raw_txn_num)

            cur_code = cls.to_str(row.get("raw_fund_code"))

            if cur_pol is None or cur_day is None or cur_txn is None:
                continue

            # locate policy
            pol_key = None
            for k, pdict in pols.items():
                if pdict.get("num") == cur_pol:
                    pol_key = k
                    break
            if pol_key is None:
                continue  # do not insert

            # locate day
            day_key = None
            for dk, dd in pols[pol_key].get("days", {}).items():
                dval = dd.get("date")
                dval = pd.to_datetime(dval).date() if isinstance(dval, str) else dval
                if dval == cur_day:
                    day_key = dk
                    break
            if day_key is None:
                continue  # do not insert

            # locate transaction
            txn_key = None
            for tk, td in pols[pol_key]["days"][day_key].get("transactions", {}).items():
                tnum = td.get("transaction_num")
                if str(tnum) == str(cur_txn):
                    txn_key = tk
                    break
            if txn_key is None:
                continue  # do not insert

            # update policy/day/txn from row (DB variants)
            p_obj = Policy(**pols[pol_key]); p_obj.update_from_row_db(row); pols[pol_key] = p_obj.to_dict()
            d_obj = Day(**pols[pol_key]["days"][day_key]); d_obj.update_from_row(row)
            pols[pol_key]["days"][day_key] = d_obj.to_dict()

            t_obj = Transaction(**pols[pol_key]["days"][day_key]["transactions"][txn_key])
            t_obj.update_from_row_db(row)

            # update fund only if it exists (no inserts)
            if cur_code is not None and "funds" in t_obj.__dict__ and t_obj.funds:
                found_idx = None
                for idx, fdict in t_obj.funds.items():
                    if fdict.get("raw_fund_code") == cur_code:
                        found_idx = idx
                        break
                if found_idx is not None:
                    from policy_structure.Funds import Funds
                    f_obj = Funds(**t_obj.funds[found_idx])
                    f_obj.update_from_row(row)
                    t_obj.funds[found_idx] = f_obj.to_dict()

            pols[pol_key]["days"][day_key]["transactions"][txn_key] = t_obj.to_dict()

        return pols