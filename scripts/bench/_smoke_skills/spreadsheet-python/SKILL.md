---
name: spreadsheet-python
description: Edit SpreadsheetBench xlsx files in Python. Read input.xlsx, write solution.py that uses INPUT_PATH and OUTPUT_PATH.
---

Use Read and Bash only. There is no Write or Edit tool.

Write `solution.py` with a Bash heredoc. The driver injects INPUT_PATH and OUTPUT_PATH; do not reassign them.

Read the workbook with openpyxl or pandas. Write computed values, not unevaluated formula strings. Preserve cells that the instruction does not ask to change.

Then run `python run_solution.py` and confirm `output.xlsx` exists.
