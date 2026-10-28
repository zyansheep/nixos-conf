#!/usr/bin/env bash
# Wi-Fi throughput for Waybar, averaged over the last 30 s: one JSON line
# every 2 s from the interface byte counters. Builtins only in the loop
# (Waybar's exec PATH is just /run/current-system/sw/bin).

step=2
window=15 # intervals of $step seconds = 30 s

# 1234567 -> 1.2M, 80123 -> 80K, 512 -> 512B (decimal units, like the sidebar)
human() {
  local value=$1 div=1 unit=B
  for next in K M G; do
    ((value < div * 1000)) && break
    div=$((div * 1000))
    unit=$next
  done
  if ((div == 1)); then
    printf -v REPLY '%d%s' "$value" "$unit"
  else
    local tenths=$((value * 10 / div))
    if ((tenths >= 100)); then printf -v REPLY '%d%s' $((tenths / 10)) "$unit"
    else printf -v REPLY '%d.%d%s' $((tenths / 10)) $((tenths % 10)) "$unit"; fi
  fi
  # Pad to four columns with figure spaces (digit-wide) so the module never
  # changes width: " 47K", "1.5K", "512B".
  local pad=$((4 - ${#REPLY}))
  while ((pad-- > 0)); do REPLY=$'\u2007'$REPLY; done
}

# A pipe nobody writes to: `read -t` on it is a fork-free sleep.
exec 3<> <(:)

times=() rxs=() txs=()
while :; do
  iface=
  for dir in /sys/class/net/wl*; do [[ -e $dir ]] && iface=${dir##*/} && break; done
  state=down
  [[ -n $iface ]] && read -r state < "/sys/class/net/$iface/operstate"
  if [[ $state != up ]] ||
     ! read -r rx < "/sys/class/net/$iface/statistics/rx_bytes" ||
     ! read -r tx < "/sys/class/net/$iface/statistics/tx_bytes"; then
    times=() rxs=() txs=()
    echo '{"text":"","class":"down"}'
    read -rt "$step" -u 3
    continue
  fi
  now=${EPOCHREALTIME/[.,]/}
  # Counters restart when the interface is recreated.
  if ((${#rxs[@]} > 0)) && ((rx < rxs[-1] || tx < txs[-1])); then times=() rxs=() txs=(); fi
  times+=("$now") rxs+=("$rx") txs+=("$tx")
  if ((${#times[@]} > window + 1)); then
    times=("${times[@]:1}") rxs=("${rxs[@]:1}") txs=("${txs[@]:1}")
  fi

  n=${#times[@]}
  if ((n >= 2)); then
    span=$((times[-1] - times[0]))
    down=$(((rxs[-1] - rxs[0]) * 1000000 / span))
    up=$(((txs[-1] - txs[0]) * 1000000 / span))
    last=$((times[-1] - times[-2]))
    now_down=$(((rxs[-1] - rxs[-2]) * 1000000 / last))
    now_up=$(((txs[-1] - txs[-2]) * 1000000 / last))
    seconds=$(((span + 500000) / 1000000))
    human "$down"; d=$REPLY
    human "$up"; u=$REPLY
    human "$now_down"; nd=$REPLY
    human "$now_up"; nu=$REPLY
    class=active
    ((down < 1000 && up < 1000)) && class=idle
    printf '{"text":"↑%s\\n↓%s","class":"%s","tooltip":"%s · %d s average\\n↓ %s/s   ↑ %s/s\\nLast %d s: ↓ %s/s   ↑ %s/s\\nClick to open the network sidebar"}\n' \
      "$u" "$d" "$class" "$iface" "$seconds" "$d" "$u" "$step" "$nd" "$nu"
  fi
  read -rt "$step" -u 3
done
