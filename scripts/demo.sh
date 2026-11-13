set -eu
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests/cascade_states -p 'test_*.py' -q
python3 -m unittest discover -s tests -p test_analysis_inputs.py -v
PYTHONPYCACHEPREFIX="$workspace/cache" python3 -m compileall -q colorectal_cascade tests
python3 -m colorectal_cascade.commands.cascade fixtures/demo.json --db "$workspace/operations.sqlite3" > "$workspace/operations.json"
python3 -m json.tool "$workspace/operations.json"
