# NOTES: "What's true now" knowledge system

**API keys:** none. **Paid APIs:** none. **Packages:** none (Python standard library only, 3.9+).
No network calls are made; every answer comes from the snapshots.

### Running it

This repository is **public**, so the website snapshots are **not committed**. The data is shared for
this event only. Copy the kit's `snapshots_zip/` folder (the five `.zip` files) into the repo root.
On its first run the scorer unpacks them into `snapshots/`. Then run, from the repo root:

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```

If you plug the `submission/` folder into your own copy of the kit instead, no extra step is needed.

### Results

| Test set | Score | Notes |
|---|---|---|
| `eval/public_eval.jsonl` (40 practice questions) | **0.946** | 0 stale-trap hits, 0 errors, consistency 1.0 |
| `submission/tests/generalization_eval.jsonl` (67 questions, mine) | **1.0** | see below |
| `harness/selftest` (synthetic site) | **1.0** | includes the self-test conflict check |

* **Speed:** median answer 0.02 s, p95 about 0.4 s (full marks up to 2 s).
* **Ingest:** about 5–10 s for all five snapshots.

The two practice misses are known:
* `dup-005` has no `must_include` facts, so it cannot score without the optional LLM judge.
* `dup-088` expects 294 VTO users (a 2024 release about 2023), but a newer release reports 256 (2024).
  The system deliberately prefers the newer figure.

**Generalization test set.** The hidden set contains rewordings, "as of" questions and questions
about things that changed, so I wrote 67 questions of my own. Every expected value and trap value
was checked by hand against the snapshots. The set covers:
* rewordings of the practice questions, grouped for the consistency check
* other products and tiers (boat, HELOC, IRA, high-yield, business cards)
* `as_of` and "back in May" questions
* "has it changed?" questions
* ended offers
* refusals (5/1 ARM, jumbo mortgage, business line of credit, land loan, a branch with no published hours)
* entity facts and fees

It runs with the same scorer:

```bash
python harness/run_eval.py --submission submission --eval submission/tests/generalization_eval.jsonl
```

Before the last round of fixes it scored 0.866. Every fix was then checked against **all three** sets,
so a gain on one never cost a regression on another. Because I tuned against this set, I also read
answers to 20 further unseen phrasings by hand. They are not scored; see Known limitations.

## How it works

The code is in `submission/cuknow/`. `solution.py` only wires up the three contract methods.

1. **One view per snapshot (`store.py`).** Each `ingest()` creates a view of the whole site on that
   date. A page that did not change is parsed once and shared between views. A page missing from a
   later crawl is carried forward from the previous view, because crawls miss pages. It is marked
   as *carried* and trusted less. `answer(q, as_of)` picks the newest view taken on or before
   `as_of`. Dates inside the question also count: "as of May 2026" and "back in June" both work.
   With no date, the latest view is used.
2. **Cleaning and chunking (`text.py`, `docs.py`).** Lines repeated across many pages (menus,
   footers, chat widgets, captchas, article teasers) are removed. Each page gets a type: official
   rates page, product page, calculator, landing/promo page, PDF, news release, blog, or listing.
   News and blog pages also get a publication date. Pages are split into searchable passages.
3. **Rate tables become structured rows (`docs.py`, `products.py`).** Every table row with a
   percentage becomes a `Row` that records:
   * product family (certificate, auto loan, mortgage, HELOC, Visa...)
   * term in months ("60-month", "30-Year", "(120 mo)")
   * model-year range ("2024-27")
   * balance or loan-amount tier ("$10,000 – $24,999.99", "$300,000 or less")
   * qualifiers (jumbo, business, IRA Roth/Traditional, refinance, intro, special...)
   * effective date
   
   Rates stated in a sentence ("6-month certificate APY: 3.50%") become rows too.
4. **Question routing (`query.py`, `engine.py`).** A question is analysed for product, term, years,
   amounts, qualifiers and intent. It then goes to one of these handlers:
   * **Rates.** Score rows by matching product, term, tier and qualifiers. A wrong term or tier is
     rejected outright, and an unrequested qualifier is penalised, so a jumbo row never answers a
     standard question. The best *page* is taken first, then the matching rows on it. "Highest" or
     "lowest" picks the extreme. "Has it changed?" prints the history across snapshots. "Do any
     tiers pay X%?" gets a yes or no.
   * **Hours.** Lobby and drive-up schedules are aggregated across branch pages. The common schedule
     comes first, then the variants. A named branch with no published hours gets "I don't have that".
   * **Links.** Anchor text, link context and target pages are scored. Blog targets are down-weighted.
   * **Membership eligibility.** The join page's rules plus the newest description of the charter area.
   * **General.** BM25 search over passages, weighted by page type, title match, recency and
     proper nouns. Count questions prefer a number next to the asked-about word, taken from the
     newest dated release. "When" questions prefer passages that contain dates.
5. **Honest refusals.** The system declines when:
   * a rate is asked for that no rate table covers (7/1 ARM, construction loan, jumbo mortgage,
     business line of credit)
   * a term or qualifier matches nothing
   * the best passage covers too little of the question
   
   Refusal answers carry `confident=False` and contain no numbers.

## How "current" is decided (the rules)

1. **Snapshot order.** Without `as_of`, the newest snapshot wins. With `as_of`, the snapshot as it
   stood on that day wins.
2. **Page authority for rates:** official rates page > product page > PDF / landing page >
   calculator > news > blog. A calculator or promo page that disagrees with the rates page is
   reported as stale. Its value is never quoted, and it only appears in `conflict_note`.
3. **Ended offers.** Sometimes a product disappears from the rates page but survives on a guide or
   promo page (the Mortgage Rate Relief program, the 25-month special, the HELOC intro rate, Achieve
   money market). The answer is then "no longer offered", not the old number. A product's own page
   (for example `/business/credit-cards`) is still trusted for that product.
4. **Old releases.** Press-release boilerplate repeats figures (branch count, members, Forbes "fifth
   time") that drift over the years. The newest dated release wins.
5. **Qualifiers are different facts, not conflicts:** term, model year, balance or amount tier,
   jumbo vs standard, purchase vs refinance, personal vs business, Roth vs Traditional, rate vs APY.

## `conflicts()`

`conflicts()` returns up to 20 entries, merged per product and term. Each entry lists the competing
values, sources, the value we believe and the reason. There are three kinds:

* **Stale copies:** the rates page vs a calculator, landing or guide page in the latest snapshot.
* **Changes over time:** the rates page itself changed between snapshots, or an offer was removed
  while another page still shows it.
* **Press-release drift:** branch count, membership, assets, employees, counties, Forbes count.

## Known limitations

* **Unusual phrasings.** The system is rule-based, so very unusual phrasings can fall through to
  general search. There they get a relevant passage, not a precise sentence. Two examples:
  * "What's the late fee on the Platinum Visa?" returns the card's apply link, not its fee line.
  * "What happened to the Hy-Vee branch?" omits the closing date. Asking "when" gets it.
* **Fees** are answered from text search, not structured rows, so they have no change history.
* **Newest release wins.** "How many X used Y last year" with several dated releases returns the
  newest release's figure.
* **Product taxonomy.** `products.py` holds generic credit-union vocabulary. A product type it doesn't
  know falls back to inference from table words, which is less precise.

## What I would do next

1. **Optional LLM answer composer.** Keep this retrieval and selection logic. Hand only the selected
   rows and passages to a model, so it phrases the answer and cannot introduce numbers that weren't
   selected. Keep the deterministic path as the fallback when no key is set.
2. **Structured fee rows** from the fee-schedule PDFs, with history, like the rate rows.
3. **Learned ranking weights.** The page-authority and penalty weights are hand-set. With a larger
   labelled set they could be fitted instead.
4. **A small CI job** that runs all three test sets on every change.

## How this was built

This was developed with an AI coding assistant (Claude Code), which the brief encourages. The
assistant did most of the data exploration, implementation and test writing. I directed the work,
made the calls on scope (for example, no LLM layer), and reviewed the approach and results. I can
walk through any part of the code and the reasoning behind each decision.
