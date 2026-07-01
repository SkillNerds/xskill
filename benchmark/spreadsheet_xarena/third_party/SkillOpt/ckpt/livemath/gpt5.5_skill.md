# Live Mathematical MCQ Heuristics

## Option Comparison

### Meta-Options About Stronger Results
- Treat options of the form “one of the remaining options is correct, but a stronger result can be proven” as serious candidates, especially when the question asks for the strongest statement.
- If a concrete option is true but your theorem or derivation gives a strictly stronger conclusion not exactly listed, choose the meta-option rather than the weaker concrete statement.
- When options are nested by strength, rank them explicitly before answering: e.g. finite-time blowup is stronger than merely “not globally bounded”; positive stable growth is stronger than ordinary unboundedness; sharper constants, rates, exceptional-set bounds, endpoint inclusion, or full equivalences are stronger than weaker asymptotic versions.
- Compare all options before committing. The correct choice is often the strongest statement justified by the question, while nearby distractors are weaker, overstrong, or miss an equality case.
- Track exact quantifiers such as "there exists", "for every", "if and only if", and "exactly when".

## Theorem-Level Precision

- Do not add converse, realization, or classification claims unless the theorem explicitly proves them. Phrases such as “conversely,” “every such parameter occurs,” “if and only if,” or “exactly all” add strength beyond a one-way implication.
- Check whether an option weakens the conclusion by dropping a characterization, equality clause, or full equivalence.
- Check whether an option overstates the theorem by upgrading regularity, removing scale restrictions, or changing an existential statement into a universal one.

## Hypotheses

### Exact Conditions and Thresholds
- For biconditional/equivalence questions, reject conditions that are merely necessary or merely sufficient. A broader condition, such as congruence modulo a divisor instead of modulo the full modulus, is usually weaker and not equivalent unless the domain collapses the extra cases.
- For threshold conditions, verify the exact sign and endpoint: distinguish \(\mu_0\) from \(-\mu_0\), \(<\) from \(\le\), and whether the equality case belongs to the positive, zero, or negative parameter regime.
- When options differ by “for every” vs “for sufficiently large,” local vs global domains, strict vs non-strict inequalities, or dependence of constants, rank them by logical strength and match the sharpest justified version.
- Verify the hypotheses and domain carefully. Distractors often keep the theorem shape but alter the required assumptions.
- Pay close attention to equality cases, extremal conditions, and whether a result applies to the full family or only a restricted subfamily.

## Final Answer
- Output the final answer as the single option label only.

## Exact Scope and Quantitative Wording
- Distinguish global conclusions from localized or completed ones. Equivalence after localization, completion, or at each prime/scale is usually weaker than an unqualified equivalence.
- In estimate-heavy options, compare every quantitative detail: exponent, derivative index range, constants and their parameter dependence, log factors, additive terms, one-sided vs two-sided notation, and pointwise vs uniform convergence.

<!-- SLOW_UPDATE_START -->
<!-- SLOW_UPDATE_END -->