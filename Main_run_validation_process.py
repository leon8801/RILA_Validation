from __future__ import annotations

import os
import uuid
import logging
from datetime import datetime
from validation_process import run_validation_process
from post_process import run_post_process
from compare_program import run_compare_process
from logger import setup_root_logging
from demo_email import EmailSender


def main():

    config = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "UI_file_name": "RILA_Validation_UI_v1_Step1.xlsx", 
        "surr_file_path": "RILA_SC.csv",
        "input_base": r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Input_RILA",
        "output_base": r"C:\AAA_RILA_Validation_Modernization\RILA_Python\Output_RILA"
    }

    run_id = str(uuid.uuid4())
    results_basepath = os.path.join(config["output_base"], config["date"], run_id)
    os.makedirs(results_basepath, exist_ok=True)

    # --- Initialize logger ---

    log_file = setup_root_logging(results_basepath)
    logger = logging.getLogger(__name__)
    logger.info(f"Logging to: {log_file}")
    logger.info("Starting validation process...")
    start = datetime.now()

    UI_file_path = os.path.join(config["input_base"], config["UI_file_name"])
    surr_file_path = os.path.join(config["input_base"], config["surr_file_path"])

    logger.info(f"Using results_basepath = {results_basepath}")
    logger.info("Loading and reading extract")

    run_validation_process(UI_file_path = UI_file_path,
                            surr_file_path=surr_file_path,
                            results_basepath=results_basepath,
                            input_base=config["input_base"])

    logger.info("run validation process finished.")

    # run_post_process(results_basepath=results_basepath) This is the validation step that compares against system file
    logger.info("run post validation process starts.")
    total_policy_not_pass, policy_difference_list, general_summary = run_post_process(results_basepath=results_basepath)
    logger.info("run post validation process finished.")

    # Run compare program
    logger.info("run compare process starts.")
    run_compare_process(results_basepath=results_basepath, UI_file_path = UI_file_path)
    logger.info("run compare process finished.")

    end = datetime.now()
    
    elapsed = end - start

    logger.info(f"Total run time: {elapsed}")

    # from demo_email import EmailSender
    if False:
        emailer = EmailSender(
            env="PROD",  # or "UAT"
            email_tool_dir_base=r"\\pnsafsda28\mrm_root",
            logger=logger,  # optional; can be None
        )

        emailer.send_email(
            start=start,
            end=end,
            now=datetime.now(),
            config=config,
            run_id=run_id,
            total_policy_not_pass=total_policy_not_pass,
            general_summary=general_summary,
            policy_difference_list=policy_difference_list,
            results_basepath=results_basepath,
        )

    logger.info("All processes completed successfully.")

    return elapsed, results_basepath

if __name__ == "__main__":
    main()