#!/data/data/com.termux/files/usr/bin/bash
# Measures real accelerometer rate for each requested delay. Keep the phone still.
# Usage: bash rate_test.sh
for d in 5 10 20 50 200; do
  timeout 10 termux-sensor -s accelerometer -d $d > /tmp/rate_$d.json 2>/dev/null
  n=$(grep -c '"values"' /tmp/rate_$d.json)
  echo "requested delay ${d} ms -> $((n / 10)) readings/s  (distinct lines: $(grep -A3 '"values"' /tmp/rate_$d.json | sort -u | wc -l))"
done
