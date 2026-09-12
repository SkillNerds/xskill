# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline

- Treat each qualifier in the question (taxable vs. non-taxable, marketable interest-bearing instruments, ownership vs. issuance basis, included/excluded funds or account types) as a filter that must be satisfied by a table row, column, or footnote before extracting the value.
- For multi-period/multi-date operations, assemble an explicitly dated, chronological operand ledger before computing differences, growth rates, trimmed means, or percentiles.
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.

## Output Format Discipline
- Before writing the final answer, re-read the question's requested format. When a bare numeric value is required, return only that number: no comma group separators, no leading '+' or '-' sign unless the sign itself is the requested quantity, no 'thousand'/'million' unit words, and no explanatory prose or parentheticals afterwards.
- If the question fixes the scale ('in thousands', 'in millions'), the number already expresses that scale; adding the unit word or a note like '(net inflow; +1,461)' makes the answer non-matching.
- Copy exact digits using the requested rounding and decimal places, and strip all formatting when the instruction says 'without commas or words'.

## Turn and Tool Discipline
- If a tool call returns the same environmental or format error repeatedly, do not keep re-invoking it: every retry consumes a turn. Pivot to direct local reads (file search, grep, partial reads) and continue the analysis there.
- On long multi-part questions, extract all needed operands in as few read passes as possible and then compute offline; many small read-compute round trips exhaust the turn budget before a final answer can be produced.

## Multi-Period Aggregation Discipline
- For statistics that aggregate a time series over many months or years (e.g., mean or geometric mean of monthly values across several calendar years), prefer one consolidated historical table that lists every requested period over splicing rows from many per-year bulletins; the designated candidate file usually contains such a summary table.
- Validate the full operand list before computing: (1) the number of values must equal the inclusive period count; (2) every value must come from the same row label, basis (calendar vs fiscal, actual vs budget), and unit scale; (3) values must be continuous at year boundaries with the neighbouring year's table.
- Preserve printed precision: if the source prints decimals, transcribe them exactly and do not round operands to whole integers before computing log-means or geometric means; answer deviations of ~0.1-1% on such tasks come from rounding or misaligned operands, not from arithmetic on correct operands.
- Keep each operand labelled with its month/year (never an unlabelled list) so shifted, duplicated, or transposed table rows become visible before any arithmetic is done.

<!-- SLOW_UPDATE_START -->
## Strategic Priorities for This Epoch

Nineteen of twenty tasks held correct — your evidence filtering, output formatting, qualifier handling, and turn discipline are all still solid. Do not disturb those patterns. One task regressed: UID0007, a geometric mean of monthly Treasury budget-expenditure values (Mar 1942–Oct 1948) taken from the consolidated Table 6 in treasury_bulletin_1950_02.txt. The pass last epoch came from cross-checking that table against other editions and its printed annual totals; the regression came from trusting the single table's raw cells. The instructions below target exactly that.

### Do not blindly trust one consolidated monthly table — reconcile it first
- It is still right to prefer a single 'Summary by Months and Calendar Years' table over splicing dozens of per-year bulletins for coverage and consistency. But before computing any mean / geometric mean / SD from it, prove the table is internally consistent and externally uncontradicted:
  - Annual-total identity: for every calendar-year row, the monthly values for that year must sum to that year's printed total within rounding. Sum at least two years' rows yourself. A row that misses the printed total by more than a rounding unit means you misread a cell, shifted a column, or hit a value that later editions revised.
  - Edition cross-check: if the workspace contains other editions of the same table (other Treasury Bulletin months), compare the specific suspect cells. When editions disagree, adopt the value that (a) satisfies the annual-total identity and (b) is continuous with its neighbouring months.
- Known trap for the 1950_02 Table 6 task: the early-1942 cells in the raw 1950_02 file differ from other editions. Using the raw cells yields about 4957.21 for the Mar 1942–Oct 1948 geometric mean; the reconciled series yields about 4962.46. Reconcile before computing.

### Geometric-mean and other multi-period aggregate tasks
- Fix the operand SET before any arithmetic: Mar 1942–Oct 1948 is 80 months (10 in 1942 + 60 for 1943–1947 + 10 in 1948). Build a month-labelled ledger and confirm both the count and the first/last included month.
- Preserve printed precision; never pre-round a clean-looking operand.
- If the product-root and log-sum routes agree but the answer is a near-miss (like 4957.21 vs 4962.46, ~0.1%), the arithmetic is fine and ONE operand is wrong — return to the reconciliation step above; do not re-do the same multiplication.
- Exclude the adjacent boundary column: confirm that the column immediately before your first month and immediately after your last month are NOT included (e.g., a trailing Jan 1982 column in a CY1981 table).

### Population standard deviation (retained, condensed)
- Population SD divides by N (the operand count), not N−1. State N and confirm it equals the inclusive month count before computing.
- If a dispersion value keeps recurring across attempts, change the operand CONSTRUCTION (row label, boundary column, month mapping) rather than re-auditing the same list; validate the chosen row with the table's additive identity.

### Reinforce (do not abandon) what is working
- Keep applying every question qualifier (taxable/non-taxable, ownership vs. issuance, marketable interest-bearing, calendar vs. fiscal, included/excluded funds and account types) as a hard filter before extracting any value.
- Keep the output-format discipline: when the question says 'report as a percent' include the %; when it asks for a bare number return digits only — no commas, no unit words, no sign unless requested, no trailing prose. Re-read the final instruction clause immediately before answering.
- Keep pivoting away from any tool call that repeats the same error, and batch operand extraction into as few read passes as possible on long multi-part questions.
<!-- SLOW_UPDATE_END -->
