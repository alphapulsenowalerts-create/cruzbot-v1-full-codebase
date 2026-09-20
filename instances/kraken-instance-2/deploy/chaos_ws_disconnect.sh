#!/usr/bin/env bash
# chaos_ws_disconnect.sh — MANUAL 10-second network chaos helper for CruzBot Instance #2
#
# WARNING: Do NOT run --execute against a live soak / production paper loop without
# explicit user approval. Default mode is dry-run (prints instructions only).
#
# Purpose: briefly block egress to Kraken exchange hosts so the paper engine
# exercises its WebSocket / stale_tick reconnect path, then restore rules.
#
# Usage:
#   ./deploy/chaos_ws_disconnect.sh                 # dry-run (safe)
#   ./deploy/chaos_ws_disconnect.sh --execute       # block ~10s (needs root + iptables/nft)
#   ./deploy/chaos_ws_disconnect.sh --execute --i-know   # allow even if paper PID is live
#
# Expected log lines in data/paper_loop.log (Kraken wording):
#   Market data stale_tick (last_tick_age=...) — closing session and reconnecting
#   Reconnect backoff sleep ...
#   PAPER gate: cancel_all simulated open orders n=...   (if resting paper orders)
#   Market data reconnect OK (last_tick_age=...)
#   Kraken WS quote error (...); retry in ...s           (if stream_quotes is active)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${ROOT}/data/paper_loop.pid"
LOG_FILE="${ROOT}/data/paper_loop.log"
DURATION_SEC="${CHAOS_DURATION_SEC:-10}"
EXECUTE=0
I_KNOW=0

# Public Kraken endpoints commonly used for market data / WS
HOSTS=(
  "ws.kraken.com"
  "api.kraken.com"
  "ws-auth.kraken.com"
)

for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    --i-know) I_KNOW=1 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

echo "=== CruzBot Instance #2 WS disconnect chaos helper ==="
echo "ROOT=$ROOT"
echo "DURATION_SEC=$DURATION_SEC"
echo "MODE=$([ "$EXECUTE" -eq 1 ] && echo EXECUTE || echo DRY-RUN)"
echo

detect_paper_pid() {
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  fi
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    # Confirm it looks like this instance's main.py
    if tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q "cruzbot_instance_2.*main.py\|main.py"; then
      echo "$pid"
      return 0
    fi
  fi
  # Fallback: pgrep for this checkout's main.py
  pgrep -f "/workspace/cruzbot_instance_2/.venv/bin/python -u /workspace/cruzbot_instance_2/main.py" 2>/dev/null | head -1 || true
}

PAPER_PID="$(detect_paper_pid || true)"

if [[ -n "${PAPER_PID}" ]]; then
  echo "Detected live production paper PID: ${PAPER_PID}"
  if [[ "$EXECUTE" -eq 1 && "$I_KNOW" -eq 0 ]]; then
    echo "REFUSING --execute while paper loop is running." >&2
    echo "Pass --i-know if you have explicit approval to chaos-test the live soak." >&2
    echo "Do not stop/restart main.py from this script." >&2
    exit 3
  fi
  if [[ "$I_KNOW" -eq 1 ]]; then
    echo "Proceeding with --i-know (user-approved chaos against live paper PID)."
  fi
else
  echo "No live paper PID detected (ok for dry-run / offline)."
fi

echo
echo "--- Expected reconnect signals (tail -F ${LOG_FILE}) ---"
echo "  Market data stale_tick ... — closing session and reconnecting"
echo "  Reconnect backoff sleep ..."
echo "  Market data reconnect OK ..."
echo "  Kraken WS quote error (...); retry in ...s"
echo

resolve_ips() {
  local host="$1"
  getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u || true
}

print_plan() {
  echo "--- Plan (hosts → IPs) ---"
  for h in "${HOSTS[@]}"; do
    echo "  $h:"
    local ips
    ips="$(resolve_ips "$h")"
    if [[ -z "$ips" ]]; then
      echo "    (no A/AAAA resolved — DNS may be filtered)"
    else
      while read -r ip; do
        [[ -n "$ip" ]] && echo "    $ip"
      done <<< "$ips"
    fi
  done
  echo
  echo "Would block OUTPUT egress to those IPs for ${DURATION_SEC}s, then delete rules."
}

if [[ "$EXECUTE" -eq 0 ]]; then
  print_plan
  echo
  echo "DRY-RUN only. To execute (needs root + iptables or nft):"
  echo "  sudo $0 --execute [--i-know]"
  echo
  echo "Manual watch:"
  echo "  tail -n 50 -F ${LOG_FILE}"
  exit 0
fi

# --- EXECUTE path ---
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: --execute requires root (iptables/nft). Re-run with sudo." >&2
  exit 4
fi

BACKEND=""
if command -v iptables >/dev/null 2>&1; then
  BACKEND="iptables"
elif command -v nft >/dev/null 2>&1; then
  BACKEND="nft"
else
  echo "ERROR: neither iptables nor nft found; cannot block egress." >&2
  exit 5
fi

mapfile -t IPS < <(
  for h in "${HOSTS[@]}"; do
    resolve_ips "$h"
  done | sort -u
)

if [[ "${#IPS[@]}" -eq 0 ]]; then
  echo "ERROR: resolved zero IPs for ${HOSTS[*]}" >&2
  exit 6
fi

echo "Using backend=$BACKEND; blocking ${#IPS[@]} IPs for ${DURATION_SEC}s"
RULE_TAG="cruzbot2-chaos-ws"

cleanup() {
  echo "Restoring egress rules..."
  if [[ "$BACKEND" == "iptables" ]]; then
    for ip in "${IPS[@]}"; do
      iptables -D OUTPUT -d "$ip" -j REJECT --reject-with icmp-host-unreachable -m comment --comment "$RULE_TAG" 2>/dev/null || true
      if command -v ip6tables >/dev/null 2>&1 && [[ "$ip" == *:* ]]; then
        ip6tables -D OUTPUT -d "$ip" -j REJECT --reject-with icmp6-addr-unreachable -m comment --comment "$RULE_TAG" 2>/dev/null || true
      fi
    done
  else
    nft delete table inet "$RULE_TAG" 2>/dev/null || true
  fi
  echo "Cleanup done. Check ${LOG_FILE} for reconnect lines."
}
trap cleanup EXIT

if [[ "$BACKEND" == "iptables" ]]; then
  for ip in "${IPS[@]}"; do
    if [[ "$ip" == *:* ]]; then
      if command -v ip6tables >/dev/null 2>&1; then
        ip6tables -I OUTPUT -d "$ip" -j REJECT --reject-with icmp6-addr-unreachable -m comment --comment "$RULE_TAG"
      fi
    else
      iptables -I OUTPUT -d "$ip" -j REJECT --reject-with icmp-host-unreachable -m comment --comment "$RULE_TAG"
    fi
  done
else
  nft add table inet "$RULE_TAG"
  nft add chain inet "$RULE_TAG" output "{ type filter hook output priority 0; policy accept; }"
  for ip in "${IPS[@]}"; do
    nft add rule inet "$RULE_TAG" output ip daddr "$ip" reject 2>/dev/null \
      || nft add rule inet "$RULE_TAG" output ip6 daddr "$ip" reject
  done
fi

echo "Egress blocked. Sleeping ${DURATION_SEC}s (watch paper_loop.log)..."
sleep "$DURATION_SEC"
# trap cleanup runs on exit
