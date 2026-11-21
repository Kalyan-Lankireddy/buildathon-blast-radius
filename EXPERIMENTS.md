# Experiments

Track: RWX Blast Radius. Score is simulated CI seconds plus CPU seconds, lower is better.

| Version | Change | make sample (simulated) | Misses | CPU | Notes |
| --- | --- | --- | --- | --- | --- |
| v0 | every test on every commit, optimal worker count and packing | 1104.510 s | 0 | n/a | 36.8 s per commit. Safety baseline. |
| v1 | selection from coverage, class-usage and file-open maps built at the pinned commit | 185.350 s | 0 | 0.85 s | perfect-selector floor on this sample is 94.4 s |
| v1.1 | class constants narrowed to the tests that read them (read sites found in the pinned source) | 177.443 s | 0 | see below | WEEK_OFFSET edit: 11.2 s to about 2.6 s |
| v1.2 | all modules imported eagerly while labeling; import-time lines widen; replaced lines use the removed line's own tests | 170.717 s | 0 | see below | mutants: 0 misses so far |
| v1.5 | class-level setup (setUpClass, class fixtures) shared with every test in the class | 170.717 s | 0 | see below | mutant harness found 2 misses on fixture edits before this fix |
| v1.6 | import-time statements that could crash an import widen; new package files run everything; docstrings count like comments | 170.717 s | 0 | see below | practice v1.5: 207.5 s |

## Lessons
- Per-test coverage is blind to work done in child processes. `test_optimizer` and `test_executor` hand their
  cases to a `ProcessPoolExecutor`, so the lines they run were credited to no test. The labeling run replaces the
  pool with an in-process executor that also pickles arguments and results, because sqlglot pickles expressions
  through `serde`, and those lines run in every test that uses the pool.
- Class-body and module-level lines run once at import. Dialects import lazily inside the first test that uses
  them, so coverage credits those lines to a handful of tests while the class has hundreds of users.
  `tools/check_maps.py` lists the 5,000 or so lines where this happens. The selector classifies each changed line
  from the AST of the pinned file and uses coverage only for lines inside function bodies.
- Class-level setup runs once, inside the first test of the class to run on each worker, but every test in the
  class depends on it. `TestExecutor.setUpClass` loads `tpc-h.sql`; the opened file was credited to a handful of
  tests and the tests that use the loaded data were not linked to it. The mutation harness found this (two
  fixture edits, two misses). Setup-phase lines, files and classes are now shared with the whole class.
- Reading pytest's failure summary needs care: a subtest title can contain multi-line SQL, which splits one
  `SUBFAILED[...]` entry across lines. The first ground truth under-counted failures because of it.

## Mutation harness (our own patches, real test outcomes)
Patches we generate against the pinned commit, run through the real suite for ground truth, then compare with
the selector's choice. They are for evaluation only.

| Run | Mutants | Misses | Note |
| --- | --- | --- | --- |
| v1.4 maps, fixture edits | 6 | 2 | class-level setup was credited to too few tests (fixed in v1.5) |
| v1.5 maps, fixture and test-file edits | 12 | 0 | |
| v1.5 maps, source edits (operator and constant swaps, half class level) | 7 so far | 0 | 36 planned, still running |

Ground truth for the first batch was recomputed after the failure-summary parser was fixed.
- A class-body edit can fail tests that never use the class. Mutant m017 changed `prefix + q` to `prefix - q` inside a
  list comprehension in `Snowflake`'s sibling `BigQuery.Tokenizer`. It runs when the module loads and raises, so every
  test file that imports the dialect fails at collection: 1,090 tests failed, the class-users rule chose 294. Statements
  that run at import and are not plain literals now widen: the full suite in a module that `import sqlglot` loads, every
  test that imports the module elsewhere. The import graph also learned that `from sqlglot.dialects import BigQuery`
  imports `dialects/bigquery.py`.
- Docstrings: the suite has no doctests (`tests/test_docs.py` defines no pytest tests) and `__doc__` is never inspected,
  so a docstring or bare string edit only matters to tests that read the file as text. Docstring mutants failed no test.
- The harness's own parser matters. Pytest prints keyword-style subtests as `SUBFAILED(k=v, ...)`, not `SUBFAILED[...]`,
  and long titles were cut at 300 characters. `tools/eval_mutants.py` now re-reads stored failure lines and flags any it
  cannot read, so an under-counted ground truth cannot pass as a clean result.
