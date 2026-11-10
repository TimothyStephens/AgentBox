#!/usr/bin/env bash
# A stub `tm`: the Toulmin side of docs/agentbox_toulmin_integration.md with fixed values, so agentbox can be
# run against the contract before the real `tm` exists.
#
#   TM_STUB_DIR   where it keeps what it is given: start.json (what `session start` prints, written by the
#                 caller beforehand), confirm.json, end.json, ends.jsonl (every end message, one per line),
#                 calls (one line per call, in order)
#
#   tm session start --role writer --json     prints $TM_STUB_DIR/start.json
#   tm session confirm <S> --json FILE        copies FILE to confirm.json, exit 0 (TM_STUB_REFUSE_CONFIRM=1: exit 3)
#   tm session serve <S>                      runs until killed (what the real one does until `end`); TM_STUB_SERVE_EXIT=1: exits 4 at once
#   tm session end <S> --json FILE            copies FILE to end.json and appends it to ends.jsonl
set -eu
dir="${TM_STUB_DIR:?set TM_STUB_DIR}"
mkdir -p "$dir"
printf '%s\n' "$*" >>"$dir/calls"
case "${1:-} ${2:-}" in
  "session start")
    cat "$dir/start.json" ;;
  "session confirm")
    [[ "${4:-}" == "--json" ]] || { echo "tm-stub: confirm wants --json FILE" >&2; exit 2; }
    cp -- "$5" "$dir/confirm.json"
    if [[ -n "${TM_STUB_REFUSE_CONFIRM:-}" ]]; then echo "tm-stub: confirm refused by request" >&2; exit 3; fi
    ;;
  "session serve")
    if [[ -n "${TM_STUB_SERVE_EXIT:-}" ]]; then echo "tm-stub: serve exiting by request" >&2; exit 4; fi
    printf '%s\n' "$3" >"$dir/serving"
    # Stay the process agentbox started (its cmdline names `serve` and the session), so the pid-plus-cmdline
    # stop at teardown finds it; the real one exits by itself once `end` is recorded.
    trap 'kill "$child" 2>/dev/null; rm -f "$dir/serving"; exit 0' TERM INT
    sleep 3600 & child=$!
    wait "$child" ;;
  "session end")
    [[ "${4:-}" == "--json" ]] || { echo "tm-stub: end wants --json FILE" >&2; exit 2; }
    cp -- "$5" "$dir/end.json"
    python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))' "$5" >>"$dir/ends.jsonl" ;;
  *)
    echo "tm-stub: unknown command: $*" >&2; exit 2 ;;
esac
