#!/usr/bin/env bash
# Mutation testing of app/ with mutmut (repo-kit CI standard, scheduled category).
#
# Usage:
#   scripts/mutate.sh                mutate the app modules changed against origin/main
#   scripts/mutate.sh --all          mutate every app module (slow; the weekly workflow does it)
#   scripts/mutate.sh app/MODULE.py  mutate the given modules
#   scripts/mutate.sh --results      list the surviving mutants of the last run
#
# mutmut names a mutant after the module path of its file, while the tests import the app
# modules as top-level names (app/ is on sys.path). The run therefore happens in a staging
# copy, .mutmut/, with app/ as src/ (mutmut strips "src.") and mutants/app linked to the
# mutated src/, which is where the tests look for the app. Results are cached there; delete
# .mutmut/ to start over. MUTATE_BASE overrides the base of the incremental run.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
REPO=$PWD
STAGE=.mutmut
PYTHON=${PYTHON:-python3}

stage() {
    mkdir -p "$STAGE/mutants"
    # rsync keeps the mtimes, so mutmut only regenerates the modules that changed.
    rsync -a --delete --exclude '__pycache__' app/ "$STAGE/src/"
    rsync -a --delete --exclude '__pycache__' tests/ "$STAGE/tests/"
    # Some app tests read image/ files (units, configs) next to the app.
    rsync -a --delete image/ "$STAGE/image/"
    # Tests that read or parse app source files (ast.parse, read_text of a module) would read
    # the mutated files, which are many times larger and not the real source; parsing them can
    # take minutes per test. Those test files do not take part in mutation testing.
    local ignores
    ignores=$(grep -lE "(PI_DIR|REPO|ROOT)[A-Z_]* / '([a-z_]+\.py|app|web|systemd)'|ast\.parse|inspect\.getsource" \
        tests/app/*.py | sed 's/.*/"--ignore=&",/' | tr -d '\n')
    cat > "$STAGE/pyproject.toml" <<TOML
[tool.mutmut]
source_paths = ["src/"]
also_copy = ["image/"]
pytest_add_cli_args_test_selection = ["tests/app/"]
pytest_add_cli_args = [${ignores%,}]
TOML
    [ -L "$STAGE/mutants/app" ] || ln -s src "$STAGE/mutants/app"
}

report() {
    (cd "$STAGE" && "$PYTHON" "$REPO/scripts/mutation_report.py" "$@")
}

if [ "${1:-}" = "--results" ]; then
    cd "$STAGE" && exec "$PYTHON" -m mutmut results
fi

modules=()
if [ "${1:-}" = "--all" ]; then
    patterns=()
elif [ $# -gt 0 ]; then
    modules=("$@")
else
    merge_base=$(git merge-base "${MUTATE_BASE:-origin/main}" HEAD)
    while IFS= read -r file; do
        [ -f "$file" ] && modules+=("$file")
    done < <({ git diff --name-only "$merge_base" -- 'app/*.py'; git ls-files --others --exclude-standard -- 'app/*.py'; } | sort -u)
    if [ ${#modules[@]} -eq 0 ]; then
        echo "No app modules changed against ${MUTATE_BASE:-origin/main}; nothing to mutate."
        exit 0
    fi
fi

names=()
if [ ${#modules[@]} -gt 0 ]; then
    patterns=()
    for file in "${modules[@]}"; do
        case "$file" in
            app/*.py) ;;
            *) echo "Not an app module: $file" >&2; exit 1 ;;
        esac
        name=$(basename "$file" .py)
        names+=("src/$name.py")
        patterns+=("$name.*")
    done
fi

stage
echo "Mutating: ${modules[*]:-all of app/}"
status=0
(cd "$STAGE" && "$PYTHON" -m mutmut run ${patterns[@]+"${patterns[@]}"}) || status=$?
# mutmut fails when the filter matches no mutant (a module without functions to mutate).
if [ "$status" -ne 0 ] && [ ${#names[@]} -gt 0 ] && report --no-mutants "${names[@]}"; then
    echo "mutmut generated no mutants for ${modules[*]}; nothing to mutate."
    exit 0
fi
report --check-complete ${names[@]+"${names[@]}"} || {
    echo "mutmut did not finish (exit code $status)." >&2
    exit "$(( status == 0 ? 1 : status ))"
}
echo "List the surviving mutants with 'scripts/mutate.sh --results'; inspect one with"
echo "'cd .mutmut && python3 -m mutmut show <name>'."
