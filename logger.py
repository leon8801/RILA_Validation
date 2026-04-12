# logger_setup.py
import logging
import os
from datetime import datetime
import sys

def setup_root_logging(log_dir: str, level: int = logging.INFO) -> str:
   os.makedirs(log_dir, exist_ok=True)
   log_file = os.path.join(
       log_dir,
       f"validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
   )
   root = logging.getLogger()
   root.setLevel(level)
   # Clear existing handlers (important if running multiple times in same interpreter)
   root.handlers.clear()
   fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
   ch = logging.StreamHandler(sys.stdout)
   ch.setFormatter(fmt)
   fh = logging.FileHandler(log_file, encoding="utf-8")
   fh.setFormatter(fmt)
   root.addHandler(ch)
   root.addHandler(fh)
   return log_file

def create_logger(name: str = "validation_logger", log_dir: str = "./logs") -> logging.Logger:
    """
    Creates and returns a logger that writes to both console and file.

    Args:
        name: Name of the logger (module/class specific name is best).
        log_dir: Directory where log files will be stored.

    Returns:
        Configured logging.Logger instance.
    """
    os.makedirs(log_dir, exist_ok=True)

    # Create timestamped log file (e.g., validation_2025-10-10_1600.log)
    log_filename = os.path.join(
        log_dir,
        f"{name}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.log"
    )

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Prevent duplicate logs if root logger is used

    # --- Formatter ---
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    # # --- Console handler (stdout, no color) ---
    # console_handler = logging.StreamHandler(sys.stdout)
    # console_handler.setLevel(logging.INFO)
    # console_handler.setFormatter(formatter)
    # --- Console handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # --- File handler ---
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)

    # Add handlers if not already present
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    logger.info(f"Logger '{name}' initialized. Logs saved to: {log_filename}")
    return logger