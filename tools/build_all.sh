#!/bin/sh
# Rebuild maps/maps.json.gz from scratch. Needs Docker and network. Takes about 5 minutes.
#
# 1. fetch sqlglot at the pinned commit (the same commit the Dockerfile uses)
# 2. build the labeling image: the organizers' base image plus requirements.lock plus coverage
# 3. run the full test suite once with per-test line coverage and the br_plugin recorder
# 4. fold the results into maps/maps.json.gz
#
# The maps hold facts about sqlglot at that commit: which test ran which line, built which class,
# opened which file. Nothing in them comes from any commit's answers.
set -eu
cd "$(dirname "$0")/.."
COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
mkdir -p work/out
[ -f work/sqlglot.tar.gz ] || curl -sSL -o work/sqlglot.tar.gz "https://codeload.github.com/tobymao/sqlglot/tar.gz/$COMMIT"
docker build -q -f tools/labeler/Dockerfile -t br-labeler .
docker run --rm br-labeler pytest --collect-only -q -p no:cacheprovider 2>/dev/null | grep '::' > work/out/collected.txt
rm -f work/out/.coverage* work/out/meta-*
docker run --rm -v "$PWD/tools/labeler:/plugin:ro" -v "$PWD/work/out:/out" \
  -e PYTHONPATH=/src/sqlglot:/plugin -e BR_OUT=/out -e COVERAGE_FILE=/out/.coverage br-labeler \
  pytest -n 8 -q -p br_plugin -p no:cacheprovider --cov=sqlglot --cov=tests --cov-context=test --cov-report=
docker run --rm -v "$PWD/tools:/tools:ro" -v "$PWD/work/out:/out" -e BR_OUT=/out -e BR_ROOT=/src/sqlglot \
  br-labeler python /tools/build_maps.py
mkdir -p maps && cp work/out/maps.json.gz maps/maps.json.gz
# fixture field map: which tests read which field of the pair-format SQL fixtures
mkdir -p work/fields && rm -f work/fields/fields-*.jsonl
docker run --rm -v "$PWD/tools/labeler:/plugin:ro" -v "$PWD/work/fields:/out" -e PYTHONPATH=/src/sqlglot:/plugin -e BR_OUT=/out br-labeler \
  pytest -q -p no:cacheprovider -p no:xdist -p br_plugin -p fieldtrack tests/test_executor.py tests/test_optimizer.py tests/test_transpile.py
python3 tools/build_fields.py work/src/sqlglot-$COMMIT maps/maps.json.gz work/fields maps/fields.json
echo "maps/maps.json.gz and maps/fields.json updated"
