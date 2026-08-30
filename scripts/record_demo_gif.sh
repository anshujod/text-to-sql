#!/usr/bin/env bash
# Records docs/demo.gif from a scripted run of `make demo`.
#
# Drives the demo's preset flow (no live API calls -- presets replay cached
# evaluation results) via `expect`, records it with `asciinema`, and
# converts the recording to a GIF with `agg`. Needs both on PATH:
#   brew install asciinema agg
#
# Usage: ./scripts/record_demo_gif.sh [output_path]  (default: docs/demo.gif)

set -euo pipefail

OUT="${1:-docs/demo.gif}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

EXPECT_SCRIPT="$TMP_DIR/record_demo.exp"
CAST_FILE="$TMP_DIR/demo.cast"

cat > "$EXPECT_SCRIPT" << 'EXPEOF'
#!/usr/bin/expect -f
set timeout 40
log_user 1

spawn uv run python -m t2sql.demo

# Beat 1+2: baseline answers silently (customer_count=5000), detects the
# ENTITY ambiguity, asks, resolves to a visibly different number (4893).
expect "Pick a question"
sleep 3
send "3\r"

expect "> your answer:"
sleep 20
send "users\r"

# Beat 3: a near-miss question where the system correctly declines to ask.
expect "Pick a question"
sleep 25
send "4\r"

expect "Pick a question"
sleep 30
send "q\r"

expect eof
EXPEOF
chmod +x "$EXPECT_SCRIPT"

asciinema rec --window-size 100x38 --command "expect '$EXPECT_SCRIPT'" --overwrite "$CAST_FILE"

agg --theme github-dark --font-size 14 --idle-time-limit 30 --speed 1 "$CAST_FILE" "$OUT"

echo "wrote $OUT"
