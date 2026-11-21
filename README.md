# Blast Radius: pick the tests a change can break

For each patch to sqlglot (pinned commit `eb11aec`), print the tests worth running and how to split them over
up to 8 workers. A commit costs its busiest worker's recorded seconds plus 2 s per worker, plus 300 s when a
failing test is left out, so the selector is built to **never miss** and then be as small as it can.

## How a patch becomes a test list

The patch is read line by line, and each changed line is classified from the AST of the pinned file:

| Where the line is | Tests selected |
| --- | --- |
| inside a function body | the tests that executed that statement (per-test line coverage) |
| a function header or decorator | the tests that executed any line of the function |
| a class body statement | the tests that built that class (or a subclass) and its dialect |
| `NAME = ...` in a class body | those tests, narrowed to ones that ran a line reading `NAME` |
| one entry of a generator's `TRANSFORMS` | the tests that looked that key up in the class's table or its cached dispatch copy |
| a module-level statement | the tests that ran code in the module, built its classes, or did so for its importers |
| a line that also runs while modules load | the above, widened to the module's users |
| a statement that runs at import and is not a plain literal | the patched package is imported in a subprocess; if that fails, the module's users and everything that imports it (the full suite in a module `import sqlglot` loads); if it imports cleanly, the normal rule for the line |
| comments or blank lines only | the tests that read that file as text |
| a data file (fixtures) | the tests that opened it; for an edit that plainly changes one half of a pair-format `.sql` fixture, only the tests that read that half |
| anything we cannot read | the full suite |

The selected tests are packed onto 1 to 8 workers, choosing the count that minimizes
`busiest worker + 2 s * workers`.

## The local check (v1.12)

For a commit whose edits are all inside function bodies of package files, where a test of 20 s or more is selected and
cheap tests (under 0.5 s) cover the changed lines, the selector runs those cheap tests on the patched code in one pytest
process (br/verify.py, with br/verify_plugin.py serving the patched sources over the read-only disk). If every one
passes, the heavy tests (3 s or more) and the verified cheap tests leave the CI selection. Any failure, error, skip,
timeout, CPU kill (10 s), crash or exhausted budget (half the time limit) keeps the normal selection. It only starts
when it could save at least 8 s of CI time, and it stops trying when wasted runs outnumber twice the passes by 5. The
image installs requirements.lock so sqlglot's tests can run. Set BR_VERIFY=0 to turn it off.

Edits to code that fills a process-wide cache (generator.py's dispatch table) now widen to the module's users, because
the coverage map credits only the first test that filled the cache (found by the heavy-test mutants h085 and h086).

## Where the maps come from

`maps/maps.json.gz` holds facts about sqlglot at the pinned commit, produced by `tools/build_all.sh`:
which test ran which line, which classes each test built, which files it opened, who imports whom. They are
measured by running the real test suite once under the organizers' image. Nothing in them comes from any
commit's answers, and nothing maps a patch or a kind of edit to a result.

Two details matter for correctness (see `EXPERIMENTS.md`): the suite runs process pools in process while
labeling (coverage cannot follow child processes, and the pool pickles results through sqlglot's `serde`),
and every dialect module is imported before any test runs, so import-time lines are recorded as import-time.

## Checking it

- `make sample` runs the image under the scorer's limits on the 30 sample commits.
- `python -m pytest checks` runs unit checks of the patch parser, packing and scope rules.
- `tools/mutate.py`, `tools/run_mutants.py`, `tools/eval_mutants.py` generate patches of our own, run the real
  suite on each for ground truth, and count the selector's misses. Those patches are for evaluation only.
- `tools/check_maps.py` lists class-body lines where per-test coverage disagrees with the class's users.

## Layout

`selector.py` entry point, `br/` the analysis (`patchparse`, `pytree`, `analyze`, `packing`), `maps/` the
measured maps, `tools/` how they are built and how the selector is evaluated, `Dockerfile` the image (sqlglot
is fetched at the pinned commit during the build).
