"""
Convert each .py file in this directory (except itself) to its own .txt file.
E.g. Policy.py -> Policy.txt, Funds.py -> Funds.txt, etc.
"""
import os
import glob
from datetime import datetime

SELF_NAME = os.path.basename(__file__)

def convert_to_txt(directory: str) -> int:
    py_files = sorted(glob.glob(os.path.join(directory, "*.py")))
    py_files = [f for f in py_files if os.path.basename(f) != SELF_NAME]

    for py_path in py_files:
        txt_path = os.path.splitext(py_path)[0] + ".txt"
        with open(py_path, "r", encoding="utf-8") as src:
            content = src.read()
        with open(txt_path, "w", encoding="utf-8") as dst:
            dst.write(content)
        print(f"  {os.path.basename(py_path)} -> {os.path.basename(txt_path)}")

    print(f"Converted {len(py_files)} files in {directory}")
    return len(py_files)

if __name__ == "__main__":
    convert_to_txt(os.path.dirname(os.path.abspath(__file__)))
