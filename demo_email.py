# demo_email.py

import os
import subprocess
from typing import List, Optional
import pandas as pd
from datetime import datetime


def _df_to_text(df: pd.DataFrame, max_rows: Optional[int] = None) -> str:
    """
    Render a DataFrame as plain-text table for email body.
    Optionally limit rows to keep emails concise.
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return "(no data)"
    if df.empty:
        return "(empty table)"
    out = df
    if max_rows is not None and max_rows > 0 and len(out) > max_rows:
        out = out.head(max_rows).copy()
        return out.to_string(index=False) + f"\n... (showing first {max_rows} of {len(df)})"
    return out.to_string(index=False)


class EmailSender:
    """
    Corporate-email launcher that shells out to EmailTool.exe:
        EmailTool.exe "<subject>" "<body>" "<userlist.txt>"

    Example UNC defaults (adjust paths to match your environment):
      - email_tool_dir_base: r"\\pnsafsda28\mrm_root"
      - env: "PROD" or "UAT" (used to pick subfolder)
      - tool exe: \\pnsafsda28\mrm_root\<env>\EmailTool\EmailTool.exe
      - user list: \\pnsafsda28\mrm_root\<env>\EmailTool\EmailUsers.txt
    """

    def __init__(
        self,
        env: str,
        email_tool_dir_base: str,
        logger=None,
        userlist_filename: str = "EmailUsers.txt",
        tool_filename: str = "EmailTool.exe",
        body_row_limit: int = 200,  # cap rows in the email for readability
    ):
        self.env = env
        self.email_tool_dir_base = email_tool_dir_base
        self.logger = logger
        self.userlist_filename = userlist_filename
        self.tool_filename = tool_filename
        self.body_row_limit = body_row_limit

    # ---- internal helpers ----

    def _tool_paths(self) -> (str, str):
        """
        Build full UNC paths for EmailTool.exe and EmailUsers.txt
        """
        tool_dir = os.path.join(self.email_tool_dir_base, self.env, "EmailTool")
        exe_path = os.path.join(tool_dir, self.tool_filename)
        userlist_path = os.path.join(tool_dir, self.userlist_filename)
        return exe_path, userlist_path

    def _log(self, msg: str):
        if self.logger is not None:
            self.logger.info(msg)
        else:
            print(msg)

    # ---- public API ----

    def send_email(
        self,
        *,
        start: datetime,
        end: datetime,
        now: datetime,
        config: dict,
        run_id: str,
        total_policy_not_pass: int,
        general_summary: pd.DataFrame,
        policy_difference_list: pd.DataFrame,
        results_basepath: str,
        email_subject_prefix: str = "[Data Validation]",
    ) -> None:
        """
        Compose the email body (and save to results_basepath/email_summary.txt),
        then invoke EmailTool.exe with (subject, body, userlist).
        """

        os.makedirs(results_basepath, exist_ok=True)
        elapsed = end - start

        # Build message body exactly as requested
        lines = []
        lines.append(f"The Oasys data validation run is finished with validation date: {now:%Y-%m-%d %H:%M:%S}")
        lines.append(f"Finished processing files: {config.get('extract_file_name_db')} and {config.get('extract_file_name_wb')}")
        lines.append(f"Total run time: {elapsed}")
        lines.append(f"Please check validation_id: {run_id} saved in {config.get('output_base')}")
        lines.append("")
        lines.append("There is general information below:")
        lines.append(_df_to_text(general_summary))
        lines.append("")
        lines.append("We found policies not passing threshold and have large differences:")
        lines.append(f"There are {total_policy_not_pass} of them.")
        try:
            # Only show failing policies (is_pass_threshold == False)
            failing = policy_difference_list[~policy_difference_list["is_pass_threshold"]].copy()
        except Exception:
            # If the flag isn't present for any reason, just print the whole table
            failing = policy_difference_list.copy()
        lines.append(_df_to_text(failing, max_rows=self.body_row_limit))
        lines.append("")
        lines.append("You may want to check the following files to debug:")
        lines.append(" - post_process_summary.txt")
        lines.append(" - data_with_rollforward.csv")
        lines.append(" - policy_difference_list.csv / policy_difference_list_all.csv")
        lines.append(" - pricing_modeling_actuarial_support python code")

        body_text = "\n".join(lines)

        # Save body to text file for auditing / re-use
        msg_path = os.path.join(results_basepath, "email_summary.txt")
        with open(msg_path, "w", encoding="utf-8") as f:
            f.write(body_text)
        self._log(f"[EmailSender] Email body written to: {msg_path}")

        # Build subject
        subject = f"{email_subject_prefix} Run finished | run_id={run_id}"

        # Resolve tool paths
        exe_path, userlist_path = self._tool_paths()

        # Basic validations with descriptive logging
        if not os.path.exists(exe_path):
            self._log(f"[EmailSender] ERROR: Email tool not found at: {exe_path}")
            return
        if not os.path.exists(userlist_path):
            self._log(f"[EmailSender] ERROR: User list not found at: {userlist_path}")
            return

        # Call the corporate tool: EmailTool.exe "<subject>" "<body>" "<userlist.txt>"
        # NOTE: If your body could be very long, some tools accept a file path instead of inline body.
        # If your EmailTool supports that, replace 'body_text' with 'msg_path'.
        cmd: List[str] = [exe_path, subject, body_text, userlist_path]
        self._log(f"[EmailSender] Launching EmailTool: {exe_path}")
        try:
            # 60s timeout is consistent with the company sample
            subprocess.run(cmd, timeout=60, check=True)
            self._log("[EmailSender] Email tool executed successfully.")
        except subprocess.TimeoutExpired:
            self._log("[EmailSender] ERROR: Email tool timed out.")
        except subprocess.CalledProcessError as e:
            self._log(f"[EmailSender] ERROR: Email tool returned non-zero exit code: {e.returncode}")
        except Exception as ex:
            self._log(f"[EmailSender] ERROR: Failed to execute EmailTool.exe: {ex}")