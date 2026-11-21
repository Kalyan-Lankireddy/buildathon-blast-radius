# Experiments

Track: RWX Blast Radius. Score is simulated CI seconds plus CPU seconds, lower is better.

| Version | Change | make sample (simulated) | Misses | CPU | Notes |
| --- | --- | --- | --- | --- | --- |
| v0 | every test on every commit, optimal worker count and packing | 1104.510 s | 0 | n/a | 36.8 s per commit. Safety baseline. |
| v1 | selection from coverage, class-usage and file-open maps built at the pinned commit | 185.350 s | 0 | 0.85 s | perfect-selector floor on this sample is 94.4 s |
| v1.1 | class constants narrowed to the tests that read them (read sites found in the pinned source) | 177.443 s | 0 | see below | WEEK_OFFSET edit: 11.2 s to about 2.6 s |
| v1.2 | all modules imported eagerly while labeling; import-time lines widen; replaced lines use the removed line's own tests | 170.717 s | 0 | see below | mutants: 0 misses so far |

## Lessons
- Per-test coverage is blind to work done in child processes. `test_optimizer` and `test_executor` hand their
  cases to a `ProcessPoolExecutor`, so the lines they run were credited to no test. The labeling run replaces the
  pool with an in-process executor that also pickles arguments and results, because sqlglot pickles expressions
  through `serde`, and those lines run in every test that uses the pool.
- Class-body and module-level lines run once at import. Dialects import lazily inside the first test that uses
  them, so coverage credits those lines to a handful of tests while the class has hundreds of users.
  `tools/check_maps.py` lists the 5,000 or so lines where this happens. The selector classifies each changed line
  from the AST of the pinned file and uses coverage only for lines inside function bodies.
