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
| v1.7 | import-time crashes follow the whole importer chain; a new method in a class body runs everything | 170.717 s | 0 | see below | harness: 53 mutants, 0 misses |
| v1.7 practice | submitted at 8:11 | | 0 | 1.06 s | practice 206.455 s total |
| v1.8 | a new method in a class body selects the class's users again (v1.7 ran everything) | 170.717 s | 0 | see below | harness: 55 mutants, 0 misses |
| v1.8 practice | not yet submitted | | | | |
| v1.9 | edits to one `TRANSFORMS` entry select the tests that looked that key up (recorded on the dispatch table) | 170.717 s | 0 | see below | dict mutants 297.8 s to 236.2 s, 0 misses |
| v1.10 | edits to one half of a pair-format SQL fixture select only the tests that read that half | 170.717 s | 0 | see below | four TPC fixture mutants 25.5 s to 4.2 / 7.9 s each, 0 misses |
| v1.11 | an import-time statement that is not a plain literal widens only if the patched package fails to import | 170.717 s | 0 | see below | mutants: 0 misses; k and s mutants 81.8 s to 26.7 s and 181.4 s to 139.1 s |

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
- Two more misses came from the structural mutants, both now closed. A string edited inside `exp.maybe_parse("""...""")`
  in a class body runs at import and breaks the DuckDB generator module; 1,112 tests failed because the failure follows
  the whole chain of importers, not two hops. And a new method added to a generator class failed tests that the
  recorded class users did not include, so a new method in a class body now runs the full suite.
- Dict-key narrowing (an edit to one entry of `TRANSFORMS`, `FUNCTIONS` or `KEYWORDS` selects only tests that looked
  that key up) is written in `tools/labeler/br_plugin.py`, `tools/build_maps.py` and `br/analyze.py`, but the committed
  maps contain no dict data, so it does nothing yet. It needs a relabel and mutants before it can be turned on.
- Harness noise: xdist workers crash when several test runs share the machine, and the heaviest tests (`test_max_nodes`,
  `test_execute_tpcds`, `test_scope`) are the ones that die. They pass on an idle machine and the organizers' labels have
  no such failures. Two mutants looked like misses only because of this, and one of them (a new method in a generator)
  made v1.7 run the full suite for any new method. `tools/eval_mutants.py` now ignores crashed-worker entries and says how
  many it dropped; v1.8 goes back to the class-users rule, which had no real miss.
- Dict-key narrowing is not worth enabling. The tables that matter (`TRANSFORMS`, `FUNCTIONS`, `KEYWORDS`) are merged into
  subclasses at import (`{**Base.TABLE, ...}`) and `TRANSFORMS` is copied once per class into a cached dispatch table
  (`generator._build_dispatch`). Recorded lookups on the original dict would miss most real lookups, so the attribute
  safety test rightly leaves them out, and what remains is a handful of dicts. Recording lookups on the dispatch table
  itself is the next idea; it needs its own mutants.
- Dict-entry narrowing for `TRANSFORMS` (v1.9). `TRANSFORMS` is merged into subclasses at import and copied once per class
  into a cached dispatch table, so recording lookups on the class dict alone would under-select. The labeling run now also
  wraps the dispatch table (`generator._build_dispatch`), the maps follow `{**Other.TRANSFORMS}` merges between generator
  classes, and a changed entry maps to the tests that looked its key up, in the class or any class it flows into. The old
  and the new key of an edited line are both mapped. The import-crash rule no longer treats an entry as risky when only
  literal values changed and its only calls (outside lambdas) are `rename_func`, which ignores its argument; calls such as
  `maybe_parse("...")` still count, because that is how a string edit once broke an import.
  Evidence: 12 `TRANSFORMS`/mapping entry mutants, 0 misses, 297.8 s down to 236.2 s; plus 3 edited keys and 3 deleted
  entries, 0 misses (those take the wide path, since a changed key is not a literal-only edit).

## Harness totals at v1.9
78 mutants with usable ground truth, 0 misses: 24 source edits, 8 test-file and fixture edits, 17 structural edits,
6 string edits and reruns, 17 dict-entry edits and reruns, 6 edited keys and deleted entries. Ten structural mutants
(base class, decorator, annotation, new file, multi-file) were left unfinished; probes of each by hand widen as intended.

- Fixture fields (v1.10). `load_sql_fixture_pairs` yields (meta, sql, expected) and the tests read them differently:
  `test_execute_tpcds` and `test_execute_tpch` never read `expected`; `test_optimized_tpch` never reads `meta`. A recorder
  (`tools/labeler/fieldtrack.py`) wraps each row and field and notes which test touches which, and `tools/build_fields.py`
  turns that into `maps/fields.json`. A fixture is used only when every test that opened it either has recorded reads or
  was seen never to touch the rows its class stored in setUpClass (26 of 26 pair fixtures qualify). The selector narrows an
  edit only when `br/fixtures.py` is sure it sits in one half: comment lines, blank lines, meta lines, a moved `;` and
  edits that touch both halves all keep "every test that opened the file". Blind spot: a C extension that reads a string
  buffer directly (duckdb.execute(sql)) is invisible to the recorder; for the executor tests the code ignores `expected`
  by unpacking it into `_`. Only the two TPC files change: the other 24 fixtures are read in full by every test.

- Import check (v1.11). A statement that runs at import and is not a plain literal used to widen to every test that imports
  the module, or to the full suite in a core module. `br/importcheck.py` now imports `sqlglot` and every module under it
  in a subprocess, with all of the commit's patched files served by a loader hook (the disk is read-only). A clean import
  drops the widening; any failure, timeout or error keeps it. m001 and m017, the two import-crash mutants, still widen
  (TypeError). Costs about 0.5 s of CPU per risky commit with the package precompiled.
- Derived attributes (built, not shipped). `tools/derive.py` follows the constants `_Dialect.__new__` reads into the class
  attributes it writes, and `BR_DERIVE=1 tools/build_maps.py` narrows such a constant to the tests that read it or anything
  derived from it. It narrowed nothing on the sample: NUMERIC_ESCAPES feeds ESCAPED_SEQUENCES, which the generator reads
  for every string literal, so its readers are the same tests as the class's users. It reaches only rare constants
  (FORMAT_MAPPING, CREATABLE_KIND_MAPPING). Building it exposed a bug worth keeping in mind: once a dict constant became
  narrowable, dict-entry narrowing switched on for it, but the metaclass reads which keys exist at import, so lookups at test
  time say nothing about an edit. Names narrowable only through derivation are now excluded from entry narrowing.
- The new-method rule (a new method in a class body selects the class's users) was first validated with xdist and four
  dropped crashed-worker entries. A clean serial rerun of those two mutants is queued behind the campaign.

## v1.12: the local check, and a miss it uncovered
- Campaign (140 mutants aimed at heavy tests, ground truth with heavy tests run serially): 0 hidden heavy failures in
  86 real ones, 0 tests that passed locally but failed in ground truth. The gated policy priced at a net +14 to +23 s
  on mostly destructive mutants, and pays on commits that break nothing.
- Sample through the amd64 image under the scorer's limits: 128.78 s simulated plus 18.7 s CPU (v1.11: 170.72 + 1.94),
  0 misses, stable over 3 runs. Two commits (2haz, 66ks) drop their heavy test; two attempts fail and fall back.
- All 140 heavy mutants through the image with no cap on attempts: 133 attempts, 21 passed and dropped heavy tests,
  none of those 21 had a heavy test really fail, 0 misses against the known truth.
- Lesson: h085 and h086 (edits in generator.py's _build_dispatch) were real misses of v1.11, unrelated to the check.
  A process-wide cache credits coverage to the first test that filled it. Fixed with the cache rule.
- s014 and s015 (new method) on clean serial ground truth: s014 fails 7 distinct tests, all in the selection; s015 fails none.
