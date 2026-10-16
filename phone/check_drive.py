#!/usr/bin/env python3
"""Quality check for one drive (stdlib only; runs on the phone or the laptop).
Usage: python check_drive.py D001      (looks in ~/rcm/drives)
       python check_drive.py path/to/file.csv"""
import csv, glob, json, os, sys

DRIVES = os.path.expanduser("~/rcm/drives")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
    if arg.lower().endswith(".csv"):
        path = arg
        drive_dir = os.path.dirname(os.path.abspath(path))
        drive_id = os.path.basename(path).split("_")[0]
    else:
        drive_dir, drive_id = DRIVES, arg
        path = glob.glob(os.path.join(DRIVES, drive_id + "_*.csv"))
        path = [p for p in path if not p.endswith("_markers.csv")]
        if not path:
            sys.exit("No CSV for " + drive_id)
        path = path[0]

    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) < 10:
        sys.exit("FAIL: file has only %d rows" % len(rows))

    ts = [int(r["timestamp_ms"]) for r in rows]
    dts = [b - a for a, b in zip(ts, ts[1:])]
    dur = (ts[-1] - ts[0]) / 1000.0
    hz = len(rows) / dur
    gaps = [d for d in dts if d > 1000]
    back = sum(1 for d in dts if d < 0)
    p95 = sorted(dts)[int(0.95 * (len(dts) - 1))]

    same = run = maxrun = 0
    prev = None
    for r in rows:
        k = (r["ax"], r["ay"], r["az"])
        if k == prev:
            same += 1; run += 1; maxrun = max(maxrun, run)
        else:
            run = 0
        prev = k
    dup_pct = 100.0 * same / len(rows)

    g = [r for r in rows if r["lat"] != ""]
    fresh = [r for r in g if r["gps_age_ms"] != "" and int(r["gps_age_ms"]) <= 2000]
    fresh_pct = 100.0 * len(fresh) / len(rows)
    accs = sorted(float(r["gps_accuracy_m"]) for r in g if r["gps_accuracy_m"] not in ("", "None"))
    med_acc = accs[len(accs) // 2] if accs else None
    sp = [float(r["speed_kmh"]) for r in g if r["speed_kmh"] != ""]
    fast_pct = 100.0 * sum(1 for s in sp if s >= 10) / max(len(rows), 1)

    res = []
    def check(name, level, msg):
        res.append(level); print("%-5s %-22s %s" % (level, name, msg))

    print("File:", os.path.basename(path), "| rows:", len(rows), "| duration: %.0f s" % dur)
    meta_path = os.path.join(drive_dir, drive_id + "_meta.json")
    if os.path.exists(meta_path):
        m = json.load(open(meta_path))
        if m.get("status") != "finished":
            check("Clean stop", "WARN", "meta says '%s' (logger was killed or still running)" % m.get("status"))
        else:
            check("Clean stop", "PASS", "finished normally")
    check("Logging rate", "PASS" if hz >= 15 else "WARN" if hz >= 8 else "FAIL", "%.1f Hz average" % hz)
    check("Timing jitter", "PASS" if p95 <= 150 else "WARN", "95%% of gaps <= %d ms" % p95)
    check("Gaps > 1 s", "PASS" if not gaps else "FAIL" if max(gaps) > 5000 else "WARN",
          "%d gaps, longest %.1f s" % (len(gaps), max(gaps) / 1000.0 if gaps else 0))
    check("Clock order", "PASS" if back == 0 else "FAIL", "%d backwards steps" % back)
    check("Repeated readings", "PASS" if dup_pct < 5 else "WARN",
          "%.1f%% identical to previous row, longest run %d" % (dup_pct, maxrun))
    check("GPS fresh (<=2 s)", "PASS" if fresh_pct >= 90 else "WARN", "%.0f%% of rows" % fresh_pct)
    check("GPS accuracy", "PASS" if med_acc is not None and med_acc <= 20 else "WARN",
          "median %s m" % med_acc)
    check("Speed >= 10 km/h", "PASS" if fast_pct >= 50 else "WARN", "%.0f%% of rows" % fast_pct)

    mk = os.path.join(drive_dir, drive_id + "_markers.csv")
    if os.path.exists(mk):
        marks = [int(r["timestamp_ms"]) for r in csv.DictReader(open(mk))]
        out = [t for t in marks if t < ts[0] or t > ts[-1]]
        check("Markers in range", "PASS" if not out else "FAIL",
              "%d markers, %d outside the drive" % (len(marks), len(out)))
    else:
        check("Markers", "WARN", "no markers file (needed for labelling)")

    print("\nOVERALL:", "FAIL - do not use for training" if "FAIL" in res
          else "WARN - inspect before training" if "WARN" in res else "PASS")


if __name__ == "__main__":
    main()
