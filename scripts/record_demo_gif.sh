#!/usr/bin/env bash
# Records docs/demo.gif from a scripted run of `make demo`.
#
# Drives the demo's live custom-question path (real API calls -- 2 cheap-
# model calls, a few cents) via `expect`, records it with `asciinema`, and
# converts the recording to a GIF with `agg`. Needs both on PATH:
#   brew install asciinema agg
#
# Usage: ./scripts/record_demo_gif.sh [output_path]  (default: docs/demo.gif)
# Needs OPENROUTER_API_KEY set (.env) -- this makes real, billed calls.

set -euo pipefail

OUT="${1:-docs/demo.gif}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

EXPECT_SCRIPT="$TMP_DIR/record_demo.exp"
CAST_FILE="$TMP_DIR/demo.cast"

cat > "$EXPECT_SCRIPT" << 'EXPEOF'
#!/usr/bin/expect -f
set timeout 60
log_user 1

spawn uv run python -m t2sql.demo

# A live custom question (real API calls): baseline answers silently
# (ranked by revenue), the system detects the METRIC ambiguity, asks, and
# resolving to "number of orders" instead surfaces a completely different
# top-5 -- zero overlap with the baseline's ranking.
expect "Pick a question"
sleep 3
send "c\r"

expect "Your question:"
sleep 1
send "Who is our best customer?\r"

expect "Proceed?"
sleep 2
send "y\r"

expect "> your answer:"
sleep 12
send "number of orders\r"

expect "Pick a question"
sleep 18
send "q\r"

expect eof
EXPEOF
chmod +x "$EXPECT_SCRIPT"

asciinema rec --window-size 100x38 --command "expect '$EXPECT_SCRIPT'" --overwrite "$CAST_FILE"

agg --theme github-dark --font-size 14 --idle-time-limit 20 --speed 1 "$CAST_FILE" "$OUT"

echo "wrote $OUT"
