#!/usr/bin/env python3
"""Event marker. Run in a SECOND Termux session while rcm_logger.py records.
Press a key + Enter the moment you feel/see an event:
  p = pothole   s = speed breaker   r = rough patch   Enter alone = unknown event
  q = quit
Usage: python ~/rcm/marker.py [D001]   (default: newest drive)
Times come from the phone clock (same clock as the logger)."""
import glob, os, sys, time

DRIVES = os.path.expanduser("~/rcm/drives")
LABELS = {"": "event", "p": "pothole", "s": "speed_breaker", "r": "rough_road"}

if len(sys.argv) > 1:
    drive_id = sys.argv[1]
else:
    metas = sorted(glob.glob(os.path.join(DRIVES, "D*_meta.json")), key=os.path.getmtime)
    if not metas:
        sys.exit("No drive found. Start rcm_logger.py first.")
    drive_id = os.path.basename(metas[-1]).split("_")[0]

path = os.path.join(DRIVES, drive_id + "_markers.csv")
new = not os.path.exists(path)
f = open(path, "a", buffering=1)
if new:
    f.write("timestamp_ms,label\n")
print("Marking drive %s -> %s\n p=pothole s=speed breaker r=rough Enter=event q=quit" % (drive_id, path))
while True:
    key = input("> ").strip().lower()
    ts = int(time.time() * 1000)          # taken immediately after Enter
    if key == "q":
        break
    if key in LABELS:
        f.write("%d,%s\n" % (ts, LABELS[key]))
        print("  saved", LABELS[key])
    else:
        print("  unknown key, nothing saved")
f.close()
