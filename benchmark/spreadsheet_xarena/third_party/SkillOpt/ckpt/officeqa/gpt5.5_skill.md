# OfficeQA Skill

## Retrieval Discipline

- When an external official time-series observation is needed, prefer the source's series/data-download/table page once identified. If exact-date or guessed-value searches return empty results, stop repeating them; broaden to the official series name/code plus `data` or `download` and use the table values.

- Treat provided/oracle parsed pages as primary evidence: if they contain the relevant table and period, extract directly from them before searching elsewhere; search only for missing continuation pages, missing periods, or an official actual value not present.
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

- If the requested date range extends beyond the provided/oracle page, first enumerate the required periods and verify that every period is present in evidence. Do not compute from a partial ledger or fill missing periods from memory; retrieve continuation pages, adjacent issues, or a later issue of the same table that contains the missing dates/revisions.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.

- For Treasury financing narratives, label each amount by transaction role before calculating: offered amount, tenders/subscriptions received, tenders accepted, competitive/noncompetitive accepted, foreign or Government-account exchange tenders, refunding, and **new cash** are not interchangeable.
- When converting currencies or scales, make a direction ledger first: source table unit, source currency, exchange-rate orientation (foreign currency per U.S. dollar means divide by the rate; U.S. dollars per foreign unit means multiply), and requested final unit.

- For tables, align values by row label and exact column header, not proximity alone; watch for continued or unlabeled columns, footnotes, adjacent amount-versus-percent columns, fiscal-year versus calendar-year sections, and repeated month rows under different year blocks.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

- For derived comparisons, preserve the direction and sign implied by the wording: “change from A to B” means B minus A; “former than latter” means former minus latter; “share accounted for by X” means X divided by the stated total; paired “gap” questions require computing each within-row difference before ranking.
- For statistical, regression, correlation, and growth-rate questions, write a formula ledger before calculating: confirm the exact series/endpoints, ordered vector, elapsed intervals, and requested convention such as continuously compounded rate, CAGR, Pearson correlation, or OLS index/year choice.
- For multi-stage questions where one table determines the period/entity used in another lookup, freeze that derived key with evidence first, then retrieve the second measure only for that exact month/year/reporting date/entity.

- For inclusive time-series ranges, make a period-by-period ledger covering every requested month/year exactly once, preserving calendar versus fiscal basis, end-of-month or end-of-fiscal-month status, source units, and any specified adjustments.

- For statistical transforms over time-series windows, confirm endpoint inclusion/exclusion exactly as worded, use consecutive time indices for trend regressions when appropriate, sort values before medians, and for logarithmic growth use ln(final/initial) before converting to the requested percentage format.

## Final Answer Discipline

- Before finalizing, enforce the requested unit and format: convert thousands/millions/billions or full nominal dollars as needed, then apply no-comma, fixed-decimal, whole-number, or nearest-tenth/thousandth formatting exactly as asked.
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.

## Statistical and Time-Series Calculation Checks

- Before computing any statistic, write the intended formula and denominator convention. If the prompt explicitly says **population standard deviation**, divide by `n`; if it says **sample**, divide by `n-1`; for a z-score comparing one observation against a small set of comparison months/periods and no population convention is stated, estimate dispersion with the **sample** standard deviation of the comparison set. Do not round intermediate operands, weighted averages, logs, exchange-rate conversions, or standard deviations before the final requested rounding.
- For long inclusive ranges, first enumerate the expected count of observations and the first/last period, then verify the ledger has exactly that count. Exclude totals, cumulative-to-date columns, comparable-period columns, estimates, and extra latest-month columns outside the requested calendar or fiscal range.
- When a page contains multiple nearby sections with similar labels, use only the section whose title and row label match the requested measure exactly; do not compute from the first visible table if the requested measure/table title is absent or only partially shown.
- For Treasury security quotations, obey the table's quote basis. If the table states that price decimals are 32nds, convert quotes such as `99.27` as `99 + 27/32`, not as decimal `99.27`. If a task asks for smoothing, averaging, or forecasting in a target currency using period-specific exchange rates, convert each period's observation to the target currency first unless the prompt explicitly says to compute in the source currency and convert only the final result.

## Stricter Final Formatting

- Match any requested output template exactly. Unless the prompt explicitly asks for unit words or explanatory text, return only the numeric value or requested list; do not append words such as `million`, `dollars`, `percent`, or `percentage points`. Include symbols/commas only when the prompt requests currency-formatted output or the answer format clearly requires them.

<!-- SLOW_UPDATE_START -->
<!-- SLOW_UPDATE_END -->
