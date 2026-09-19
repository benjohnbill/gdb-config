#!/usr/bin/env bash
# Every suite runs in its own gdb, started with --nx so the user's ~/.gdbinit
# cannot decide the result. Each test file exits gdb with its own status.
set -u
cd "$(dirname "$0")" || exit 1

gcc -g -O0 -o fixtures/chains  fixtures/chains.c  || exit 1
gcc -g -O0 -o fixtures/widgets fixtures/widgets.c || exit 1

failed=0
for pair in test_chain.py:chains \
            test_tree.py:chains \
            test_deep.py:widgets \
            test_each.py:widgets \
            test_track.py:chains \
            test_walk_command.py:chains; do
    file=${pair%%:*}
    fixture=${pair##*:}
    output=$(gdb --nx --batch -x "$file" "./fixtures/$fixture" 2>&1)
    echo "$output" | grep -E '^(---|  FAIL)' 
    echo "$output" | grep -q '0 failed' || failed=1
done

if [ "$failed" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "SUITE FAILED"
fi
exit "$failed"
