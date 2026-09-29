#!/bin/bash
# agentbox selftest probe. Runs inside the sandbox with the session's exact spec.
# Reads "<id> <op> [args...]" lines on stdin, prints "<id> <observation>" lines.
# It only observes: pass/fail is decided on the host. It never prints environment values.
set -u

proxy_hostport() {
  local p="${HTTP_PROXY:-${http_proxy:-}}"
  p="${p#http://}"; p="${p%%/*}"
  printf '%s' "$p"
}

# proxy_req METHOD TARGET HOSTHEADER -> HTTP status code from the proxy, or 000
proxy_req() {
  local hp; hp=$(proxy_hostport)
  [ -z "$hp" ] && { echo noproxy; return; }
  local code
  code=$(timeout 20 bash -c '
    exec 3<>"/dev/tcp/$0/$1" || exit 1
    printf "%s %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: agentbox-selftest\r\nConnection: close\r\n\r\n" "$2" "$3" "$4" >&3
    IFS= read -r line <&3
    printf "%s" "$line"' "${hp%:*}" "${hp##*:}" "$1" "$2" "$3" 2>/dev/null | awk '{print $2}')
  printf '%s' "${code:-000}"
}

# direct HOST PORT -> connected | refused | unreachable | timeout | noresolve | error:<msg>
direct() {
  local out rc
  out=$(timeout 8 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo connected
  elif [ $rc -eq 124 ]; then echo timeout
  else
    case "$out" in
      *refused*) echo refused ;;
      *"No route"*|*unreachable*|*"Network is down"*) echo unreachable ;;
      *"Name or service"*|*"not known"*|*"resolve"*|*"Temporary failure"*) echo noresolve ;;
      *) echo "error:$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-80)" ;;
    esac
  fi
}

while read -r id op a b; do
  a="${a:-}"; b="${b:-}"
  case "$op" in
    status)   r=$(grep -E '^(CapEff|NoNewPrivs|Seccomp):' /proc/self/status | tr -d ' \t' | tr '\n' ' ') ;;
    write)    if ( : >>"$a" ) 2>/dev/null; then r=ok; else r=fail; fi ;;
    mkdir)    if mkdir "$a" 2>/dev/null; then r=ok; rmdir "$a"; else r=fail; fi ;;
    exists)   if [ -e "$a" ] || [ -L "$a" ]; then r=yes; else r=no; fi ;;
    readable) if [ -d "$a" ]; then ls "$a" >/dev/null 2>&1 && r=yes || r=no
              elif [ -e "$a" ]; then head -c1 "$a" >/dev/null 2>&1 && r=yes || r=no
              else r=no; fi ;;
    count)    if [ -d "$a" ]; then r=$(ls -A "$a" 2>/dev/null | wc -l); else r=missing; fi ;;
    size)     if [ -e "$a" ]; then r=$(wc -c <"$a" 2>/dev/null || echo unreadable); else r=missing; fi ;;
    sha)      if [ -f "$a" ]; then r=$(sha256sum "$a" 2>/dev/null | cut -c1-64); else r=missing; fi ;;
    envnames) r=$(env | cut -d= -f1 | sort | tr '\n' ' ') ;;
    envmatch) r=$(env | grep -E "=$a" | cut -d= -f1 | sort | tr '\n' ' ') ;;
    envset)   if [ -n "${!a:-}" ]; then r=set; else r=unset; fi ;;
    ifaces)   r=$(ls /sys/class/net | tr '\n' ' ') ;;
    which)    r=$(command -v "$a" || echo missing) ;;
    dns)      if getent hosts "$a" >/dev/null 2>&1; then r=resolved; else r=failed; fi ;;
    get)      r=$(proxy_req GET "$a" "$b") ;;
    connect)  r=$(proxy_req CONNECT "$a" "$a") ;;
    direct)   r=$(direct "$a" "$b") ;;
    *)        r="unknown-op" ;;
  esac
  printf '%s %s\n' "$id" "$r"
done
echo "__done__ ok"
