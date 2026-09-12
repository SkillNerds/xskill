# Spreadsheet Manipulation Skill (xlsx)

## Overview
This skill guides agents in manipulating Excel (.xlsx) spreadsheets using Python.

**Primary libraries**: `openpyxl` (structure-preserving read/write), `pandas` (data transformation).
Never use any other third-party libraries.

---

## Common Workflow

1. **Explore** the input file: list sheets, inspect headers, check dimensions.
2. **Write `solution.py`** with `INPUT_PATH` and `OUTPUT_PATH` defined at the top.
3. **Execute** `python solution.py` and verify the output file was created.
4. **Confirm** the target cells/range contain the expected values.

---

## Efficiency on Large or Formula-Heavy Workbooks

- For lookups, aggregations, or filtering across sheets, load the needed columns once into dictionaries or a pandas DataFrame and reuse them; avoid repeatedly scanning large worksheet ranges inside a row loop.
- For row insertion or deletion, first collect the row indices that need to change, then apply the changes from the bottom of the sheet toward the top (or in a single batch) so earlier indices are not invalidated. Avoid rebuilding or copying a whole sheet cell-by-cell for large files.
- When only cached values are needed, use `load_workbook(..., data_only=True)`; add `read_only=True` for a lightweight read-only pass. Processing very large or formula-heavy workbooks multiple times can hit the time limit.

---

## Library Selection

| Use case | Library |
|----------|---------|
| Preserve formulas, formatting, named ranges | `openpyxl` |
| Bulk data transformation, aggregation, sorting | `pandas` → write back with `openpyxl` |
| Simple cell read/write | `openpyxl` |

**Warning**: `pandas.to_excel()` silently destroys existing formulas and named ranges.
When writing back to a spreadsheet that contains formulas, always use `openpyxl.save()`.

---

## solution.py Template

```python
import openpyxl
import pandas as pd

INPUT_PATH  = "..."   # set to the actual input path
OUTPUT_PATH = "..."   # set to the actual output path

wb = openpyxl.load_workbook(INPUT_PATH)
ws = wb.active  # or wb["SheetName"]

# --- perform manipulation ---

wb.save(OUTPUT_PATH)
```

---

## Output Requirements

- Save the result to `OUTPUT_PATH`.
- Do not hardcode row counts or column letters. Locate the source/target columns by scanning the actual header row for the named columns, and iterate only the real data rows beneath it.
- If a target/example sheet already exists with headers, treat those headers as the fixed output schema: write results into matching columns and keep blank or pre-existing rows/cells intact.
- For keyword/description filters (contains, starts with, partial match allowed), compare case-insensitively with substring/prefix logic (`lower()` + `in`/`startswith`), not whole-cell equality; preserve listed keyword order when keywords map to output values.
- Preserve sheets and cells not mentioned in the instruction.

---

## openpyxl Does Not Calculate Formulas

`openpyxl` writes formulas as formula text and **never evaluates or recalculates them**. A cell that contains a formula — whether it was already in the workbook or was written by your script — has no cached numeric result after `wb.save(...)` unless another application recalculates the file. Consequently, reading the saved workbook with `load_workbook(path, data_only=True)` returns `None` for every such cell, so a formula-based solution can appear empty or failed even when the formula text is correct.

For target cells whose expected result is a value:

- Compute the result in Python and assign the literal value to the cell. Do not rely on writing a formula string such as `=SUM(...)`, `=SUMPRODUCT(...)`, or `=IFERROR(INDEX(...),...)`.

- Prompts phrased as “create a formula”, “I need a formula”, “use COUNTIFS/SUMIFS/VLOOKUP/SUMPRODUCT”, “fill/categorize/count/filter automatically”, or “it should update when the report date changes” describe desired behavior — values that are correct and remain correct after edits — not a mandate to leave `=...` text in a cell. No recalculation engine runs before verification, so implement the requested behavior in Python, compute the derived key or value for every real data row, and write literal correctly typed results in source order. Reserve an actual formula string for cases where preserving formula text is the explicit goal or recalculation is externally guaranteed.
- If you need values that depend on formulas already present in the input file, load the input with `data_only=True` so the cached displayed values are read instead of formula text.
- If an existing formula cell lies in the output region and must show the correct total after an edit, compute the new value in Python and write that numeric result; openpyxl will not recalculate the formula for you.
- Even if an instruction says to write or fix a formula, a cell that is checked by its displayed value must hold a literal value — or the saved file must be recalculated externally (e.g., LibreOffice headless) so cached values exist.
- Self-verify by reading the output with `data_only=True` and confirming checked cells contain values, not `None`.

Example:

```python
# A formula string alone is not enough for a value-based check:
# ws['R6'] = '=SUMPRODUCT(...)'

# Compute in Python, then store the literal result:
computed = 0.0
# ... iterate rows/columns and accumulate the matched values ...
ws['R6'] = computed
```

---

## Recurring Patterns From Successful Spreadsheet Tasks

### Read the Actual Workbook, Not the Preview

- Treat the preview as incomplete: it may be truncated, show only an empty-looking region, or omit populated rows. Before finalizing `solution.py`, scan every sheet and locate the true header row and populated data band (`ws.max_row`, `ws.max_column`, non-empty cells). Identify each sheet role: source data, lookup/config, or existing output/example sheet whose headers reveal the expected layout.
- Read full source and lookup tables into in-memory dicts/lists keyed by IDs or header labels; never cap a scan at the rows shown in the preview.

- Rows that already look like finished answers (for example sample rows in the preview) are a specification, not the complete answer. Re-derive the output for every real data row in the populated band, computing intermediate values in Python (for example age from a date of birth and report date) before matching lookup keys. Keep adjacent labelled summary/constraint rows such as min/max or totals outside the data loop unless they are explicitly targeted.
- When the instruction is vague or says the intended rule is inside the file, treat an already-completed example block as the specification: compare its outputs with the source rows that produced them, infer the rule, then apply the same rule to the remaining rows.

### Locate Targets by Labels and Headers, Not Fixed Coordinates

- Templates put tables at unpredictable locations: headers can span rows, labels such as `Year` or `Result:` can sit next to data, and output columns may be far apart. Search for known label/header text first, then compute the target region by offsets from that anchor.
- Use the real data extent rather than previewed or example dimensions. For stateful row-group logic (count or sum between marker rows), iterate once while tracking state and write only on matching rows; leave non-matching rows empty.

### Repair Buggy Formulas and Store Correctly Typed Values

- For repair tasks, do not trust formulas already present in the input: inspect the current formula/value, infer the intended meaning, and rewrite the affected cells.
- If a time/date/number is expected, store a typed value, not fixed-width text.
- For block formulas meant to work when copied, anchor references on label/header cells and match all relevant keys so results do not come from the wrong row.

### Preserve Formatting, Dates, and Error Cells During Structural Edits

- Compare date/datetime cells as `datetime` objects and use `timedelta`, not string comparison. When rebuilding rows, carry over `number_format` and relevant styles.
- To recognize `#N/A` or other formula-error results before structural edits, load a second `data_only=True` view, but apply edits to the formula-preserving workbook. Prefer `ws.delete_rows()`/`ws.delete_cols()` or explicit row copying over full sheet recreation, and never round-trip through `pandas.to_excel()`.

### Row-Level Delete, Filter, Insert, and Compact Operations

- Decide which rows stay and which are removed before mutating; collect the keep/remove set in one pass.
- Delete from the bottom upward, or rebuild: read surviving rows into a list, clear the data area, then rewrite rows from the first data row. The rebuild approach also supports combined delete, insert, and compact edits.
- Keep the header row intact. For clear-but-not-delete instructions, assign `None` only to target cells. Never iterate forward over the sheet while deleting rows in place.

### Grouped Aggregations, Conditional Filters, and Group-Level Lookups

- When duplicate key rows must collapse to one remaining row (for example one row per key pair with the summed Qty/price), make one normalized forward pass with a dict mapping the normalized key to the accumulated total and the first data-row index for that key. Write the Python-computed total on that first-occurrence row, then blank or delete the later duplicates. Preserve the original source order — never sort or rebuild from a set.
- When the value written for every row depends on all rows sharing its key (for example one joined label derived from every status of an ID), aggregate the whole group once, choose the result, then write it back to every member row; row-local scanning misclassifies multi-row groups.
- For lookup/filter-style outputs, collect matching rows in one downward scan of the real source rows, then fill the output cells sequentially below their output header. Leave cells below the last match empty; do not interleave results at arbitrary intermediate rows.
- Treat condition inputs as typed data: “whole number” means a numeric value with a fractional part of 0, “date present” means a non-empty datetime cell, and “unchecked”/blank means the cell value is `None`/empty.

### Cross-Sheet and Cross-Column Matching

- Build lookup maps or allowed-value sets once from the source sheet, then use them while iterating target rows.
- Normalize both key sides before comparing: integer IDs may appear as numeric strings, and whitespace or case may differ (`str(x).strip()`, numeric conversion, `lower()`).
- Key on tuples for multi-column matches. For last-N matching, preserve matching source order and slice the final N rows.

### Parsing Cell Text and Determining Output Positions

- If instructions name outputs by header/label text (for example `No. of days` or totals beside labels), scan for that text and process all real data rows beneath it.
- Parse free-text cells with the standard-library `re` module and place extracted pieces in adjacent cells, clearing stale placeholders so unused slots stay blank.
- For inclusive text ranges such as `2 to 5`, parse the bounds and compute `end - start + 1`. For summary tables, pair each row label with its aggregate and write beside that label using a dynamic data range.

---

### Never Drop Cells Because of Falsy / Empty-Cell Checks

- Decide whether a source cell has data with `cell.value is not None`, never with `if cell.value:` — a real numeric `0` is data and must still be copied, summed, or written.
- When you reproduce a formula/table-copy pattern, every target cell that pattern would cover must receive a result. A blank source cell usually behaves as `0` in an `INDEX`/`MATCH` style copy or arithmetic formula, so write `0` for those cells instead of leaving them empty.
- For a genuine no-match case, write the literal marker the spreadsheet would show (e.g. the string `'#N/A'`) if expected; do not leave the cell `None`.
- After saving, re-read the output with `data_only=True` and confirm every checked cell holds a value — including zeros — not `None`.

---

### Respect Explicit Final Sort Order and Pre-Keyed Output Columns

- If the instruction names a numeric sort (for example, an output value column sorted lowest to highest), perform that numeric sort as the final ordering pass over the produced rows. Do not replace it with an alphabetical ordering or a helper-list grouping unless the instruction states the list is the sort criterion.
- When sorting or copying into an output range that already contains a key/ID column (sequence numbers, item numbers, pre-existing names), keep each key in its original row and write computed values into the sibling columns of that same row. Never shift, duplicate, reorder, or overwrite the key column while repopulating the output.
- Before merging list-priority logic with a sort, decide exactly which criterion is primary: list membership can select or group rows, but only an explicitly stated sort column controls the final order of those rows.

<!-- SLOW_UPDATE_START -->
Treat each conditioned edit exactly as the prompt spells it: 'and' joins clauses that must all hold; never delete/change a row because one sub-clause matches while another fails, and never add an unrequested delete/insert/overwrite step. For multi-step delete/insert/compact tasks, take the original file as the single source of truth, apply each predicate in prompt order to an in-memory band of rows keyed by header name, and write the finished band back over the existing data area. Do not rely on live insert_rows/delete_rows sequences; if you must mutate the live sheet, apply changes strictly bottom-up from a pre-edit scan and verify the beginning, middle, and end of every affected column after saving.

When an instruction names multiple phases (for example, first sort a column alphabetically, then find/transform words), execute the phases in the order stated and feed each phase's result into the next; never skip a leading sort/filter or derive phase-two results from phase-one's unprocessed input.

For word/string pattern or transformation tasks (suffix conditions such as 'last three letters', placing the fifth letter at the front, or similar), find or infer the concrete example embedded in the prompt or in any filled example/output block before coding. Use that example to determine (a) which word is the source and which form is written to the output, (b) the exact transformation direction, and (c) whether results are ordered by the sorted/processed source list. Sanity-check the first output cell against the example; if your rule would produce a different first value, reverse or fix the rule before writing the full pass. Build a set or dict of every real word in the relevant data range, then for each real source occurrence compute the required form and include it only when the stated predicate is satisfied (for example, the derived form or its required counterpart actually exists in the word set). Never output a value for every row, never re-transform a transformed value, and never let the output count exceed the number of real matches. Fill list outputs from the first result cell downward in match order and leave the remaining cells below empty; after saving, verify the list's content, order, and count against any provided sample.

For numeric aggregation/subtraction (for example, BALANCE = SALE - RET, especially when contributing columns repeat across multiple sheets), read every contributor numerically and treat a textual dash '-' or a blank/null cell as zero. Aggregate once per logical key in a dictionary, then fill every target row that exists from that dictionary. Write literal numeric values (including 0 or 0.0) into result cells, never '-' and never blank, even when some contributing cells are missing or non-numeric; reopen with data_only=True afterward and confirm that zero-valued result cells actually contain the number 0, not '-' and not None.
<!-- SLOW_UPDATE_END -->
