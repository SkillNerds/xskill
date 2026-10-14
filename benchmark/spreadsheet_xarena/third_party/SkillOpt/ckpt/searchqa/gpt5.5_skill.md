# Question Answering Skill

(No learned rules yet. Rules will be added through the reflection process.)

## Concise Answer Normalization
- Prefer the shortest unambiguous answer that directly satisfies the question. Do not include generic descriptors, legal suffixes, or expanded formal names unless the question specifically asks for the full official name or the descriptor is necessary to identify the entity.

- If the answer appears inside a longer descriptive phrase, strip words that merely repeat the clue's requested type or modifiers already stated in the clue. For short-answer trivia, return the distinctive core entity or headword rather than role titles, product flavor adjectives, or place/facility designators, even when those words are part of a fuller official phrase, unless the full official name is explicitly requested.
- For place/name-etymology questions asking for “the name” or “the word” that means something, answer the distinctive name/word itself rather than a larger phrase with a generic type label.

- For natural geographic features, preserve conventional feature designators such as “Lake,” “River,” “Bay,” “Gorge,” “Mount,” or “Island” when they are part of the proper name or match the requested feature type. Do not shorten “Lake Okeechobee,” “Tampa Bay,” or “Olduvai Gorge” to an ambiguous base name merely to be concise.
- For companies, brands, and organizations, answer the common distinctive name when sufficient; omit additions such as “Company,” “Corporation,” “Inc.,” etc. unless explicitly required.

- Preserve the answer surface form supported by the strongest evidence when exact variants differ: spelling, capitalization, punctuation, and word order can matter. Do not substitute an equivalent official/common variant such as an alternate spelling or inverted institution name if a direct title/snippet/answer field gives the expected form.

- When copying titles or quoted names, preserve ordinary ASCII punctuation from the evidence, especially straight apostrophes (`'`). Do not replace them with typographic curly quotes/apostrophes unless that exact stylized form is explicitly shown as the supported answer.

- For nicknames, epithets, saints, and quoted titles, copy the supported surface form exactly, including spacing, capitalization, and conventional abbreviations such as “St.” Do not normalize a stylized or quoted form into a lowercase dictionary word or an expanded spelling when the clue/evidence points to the stylized answer.

- For person answers in trivia or crossword-style clues, prefer the conventional supported name. Use just a surname, first name, or saint/regnal name only when the clue/source clearly expects that short form; otherwise use the canonical full personal name from the strongest evidence or answer field, especially when a lone given name would be ambiguous.
- Return the grammatical base form expected by the clue. Do not add a plural `s` merely because a crossword source pluralizes a shared name or category; if the clue lists people sharing a first name, answer the singular given name.

- For common-noun category answers, default to the singular dictionary headword in trivia/crossword-style clues, even if the clue uses plural words like “these,” “those,” “places,” or “items” for grammar. Use a plural only when the term is inherently plural or an answer field/source clearly gives a plural phrase.

- For common-noun clues about things being replaced, used in place of, or substituted by another system/item, answer the broad headword for the thing replaced unless a narrowing modifier is required by the clue or answer field. Do not add adjectives such as “letter,” “regular,” or “standard” merely because they appear in explanatory context.
- For fill-in-the-blank or definitional clues using words like “this” or “that,” provide a standalone noun phrase. Avoid context-dependent pronouns or possessives from the source text; use a natural article such as “the” when needed (e.g., answer “the highest point,” not “its highest point”).

## Context-Grounded Evidence Matching
- Start by identifying the most distinctive terms in the question: proper names, dates, titles, quoted phrases, unusual words, roles, relationships, and category descriptors.
- Prioritize passages or document titles where several distinctive clue terms occur together, especially if the wording directly repeats or closely paraphrases the question.
- Treat document titles as useful evidence: the answer is often named in a title while the snippet confirms the clue facts.

- Do not assume the document title itself is the answer. If the requested type differs from the title entity, use the title as context and extract the matching typed entity from the snippet or clue relationship.

- For “known as,” “called,” “defined as,” or category/type clues, choose the canonical term explicitly used in the strongest matching title/snippet or scraped answer field rather than inventing a related derivative or near-synonym from the clue wording. When multiple plausible candidates appear, prefer the candidate whose evidence directly states the requested relationship and repeats the most distinctive clue facts.
- Ignore noisy results that only match generic words; prefer evidence that directly connects the clue facts to one specific entity.

## Clue Interpretation and Answer Type
- For Jeopardy-style wording such as “this man,” “this group,” “this film,” “this country,” “this system,” “he,” or “his wife,” infer the expected answer type before choosing the answer.
- Use that expected type to validate candidates: answer with the concise person, place, title, organization, object, term, or phrase requested by the clue.

- Treat modifiers attached to the requested type as hard filters, not background flavor: constraints like dates, “largest,” “2-letter-named,” “1978 remake,” “hot dog brand,” “dual throne,” or “on this company’s board” must all fit the candidate before you answer.
- For clues centered on creative works such as books, films, plays, songs, poems, or other media, first determine whether the clue asks for the work itself, its creator, a performer or cast member, a character, a quotation source, or a setting. Verbs such as “wrote,” “directed,” “stars,” “played,” and “set in,” plus pronouns like “he” or “her,” usually determine the target.

- For fill-in-style clues with placeholders such as “this,” “these,” or “one of these,” substitute each candidate back into the clue and choose the concise answer that makes the full phrase, title, or fact read correctly.
- For terse clues that are just examples or names separated by commas, slashes, or “or,” infer the shared category, class, or synonym that links them, then answer with that concise common term.

- For crossword-style clues, treat parenthetical numbers or stated letter counts as hard constraints on the answer length, and omit generic labels that would violate them. In dual-definition clues using wording like “X, or what Y does,” choose the single word that satisfies both senses and preserve the required inflected form.
- If the clue references an unavailable image or link with wording like “seen here,” “pictured,” or parenthetical visual hints, rely on the textual clues and context to infer the answer; do not treat the missing image as necessary evidence.
- If multiple snippets support the same entity, use that corroboration to choose the canonical/common form of the answer.

## Trivia / Jeopardy Snippet Formats
- Retrieved trivia snippets may contain the clue and answer in scraped formats such as `CATEGORY | clue | answer`, `clue. ANSWER`, or labels like `right:`.
- When the question text matches the clue in such a snippet, extract the answer field or adjacent answer name, not the category or the whole clue sentence.

## Common Clue Traps
- Watch for inverse relationships: if the clue says “His third wife was Jiang Qing,” the requested answer is the husband, not Jiang Qing.

- More generally, preserve relation direction in clues: “A is evidence of this B,” “A is related to this language,” or “home to these characters” asks for the target of the relationship, not the entity already named in the clue.

- When a clue says examples, models, breeds, members, or items “include,” “like,” or “such as” named entities, treat those names as evidence for the requested parent class or entity. Answer the encompassing brand, animal, category, place, or term requested by “this,” not one of the examples already given.
- If the question gives the start of a quotation or phrase, answer with the exact missing continuation from the context.

- For song, poem, nursery-rhyme, or quotation clues, first decide whether the question asks for a missing word or phrase from the quote or for the associated creator, performer, or work; use pronouns and answer-type signals to choose the right target.
- When a clue asks for a constrained form such as a first name, abbreviation, acronym, or lyric word, return that exact form rather than the fuller person, title, or explanation; preserve conventional punctuation or spelling when it is part of the requested form.
- If the clue contains wordplay, quotation marks, or puns, treat them as hints, but answer with the real entity supported by the evidence.

- If a clue includes a quoted title, quoted narration or lyric, named event, slogan, or other distinctive phrase but asks for an associated “this” entity, treat the quote or name as evidence to identify the requested person, work, place, group, category, source, or term; do not return the quoted anchor unless the clue explicitly asks for it.

<!-- SLOW_UPDATE_START -->
<!-- SLOW_UPDATE_END -->
