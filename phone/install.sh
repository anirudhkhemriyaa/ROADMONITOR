#!/data/data/com.termux/files/usr/bin/bash
# Served by the laptop backend; __BASE__ is filled in automatically.
set -e
BASE="__BASE__"
echo "Installing packages..."
pkg update -y
pkg install -y python termux-api curl
mkdir -p ~/rcm/drives
echo "$BASE" > ~/rcm/server.txt
echo "Downloading project scripts from $BASE ..."
for f in rcm_logger.py marker.py check_drive.py upload.py rate_test.sh; do
  curl -fsS "$BASE/phone/$f" -o ~/rcm/$f
done
termux-setup-storage || true
echo
echo "Server check:"; curl -fsS "$BASE/ping" && echo
echo "Sensors found:"; termux-sensor -l | head -n 15
echo
echo "INSTALL DONE. Live test:"
echo "  python ~/rcm/rcm_logger.py --vehicle bike --live --gps-wait 0"
