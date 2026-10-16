#!/usr/bin/env python3
"""Road Condition Monitoring - phone logger for Termux (Python standard library only).

One run = one drive. Files go to ~/rcm/drives/:
  D001_2026-10-20_bike.csv   one row per accelerometer reading + latest GPS
  D001_meta.json             drive details and logging statistics

Example:
  python ~/rcm/rcm_logger.py --vehicle bike --phone "Redmi Note 11" --mount handlebar
Stop with Ctrl+C (the file is flushed every second, so a crash loses at most ~1 s).
"""
import argparse, csv, json, os, re, signal, subprocess, threading, time, urllib.request
from datetime import datetime

BASE = os.path.expanduser("~/rcm")
DRIVES = os.path.join(BASE, "drives")
HEADER = ["timestamp_ms", "ax", "ay", "az", "lat", "lon",
          "speed_kmh", "gps_accuracy_m", "bearing_deg", "gps_age_ms"]

stop = threading.Event()
procs = []
gps = {"t": None, "lat": None, "lon": None, "speed": None, "acc": None, "bearing": None}
gps_lock = threading.Lock()
live_buf, live_lock = [], threading.Lock()


def live_worker(base, device):
    """Optional: send the last second of rows to the backend dashboard. The CSV file stays the real record."""
    url = base.rstrip("/") + "/api/live"
    while not stop.is_set():
        time.sleep(1)
        with live_lock:
            batch = live_buf[:]
            del live_buf[:]
        if not batch:
            continue
        req = urllib.request.Request(url, json.dumps({"device": device, "rows": batch}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass   # dropped batch is fine; never let the network slow down logging


def json_stream(proc):
    """Yield JSON objects from a process that prints pretty-printed objects back to back."""
    buf, depth = [], 0
    for line in proc.stdout:
        s = line.strip()
        if not s:
            continue
        buf.append(s)
        depth += s.count("{") - s.count("}")
        if depth <= 0:
            text, buf, depth = " ".join(buf), [], 0
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                continue


def next_drive_id():
    nums = []
    for f in os.listdir(DRIVES):
        m = re.match(r"D(\d+)_", f)
        if m:
            nums.append(int(m.group(1)))
    return "D%03d" % (max(nums) + 1 if nums else 1)


def find_accel_sensor():
    out = subprocess.run(["termux-sensor", "-l"], capture_output=True, text=True, timeout=30).stdout
    for name in json.loads(out).get("sensors", []):
        low = name.lower()
        if "accelerometer" in low and "uncalibrated" not in low and "limited" not in low:
            return name
    raise SystemExit("No accelerometer found. Is Termux:API installed?")


def gps_worker():
    p = subprocess.Popen(["termux-location", "-p", "gps", "-r", "updates"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    procs.append(p)
    for o in json_stream(p):
        if "latitude" not in o:
            continue
        with gps_lock:
            gps.update(t=time.time(), lat=o["latitude"], lon=o["longitude"],
                       speed=(o.get("speed") or 0.0) * 3.6,   # m/s -> km/h
                       acc=o.get("accuracy"), bearing=o.get("bearing"))
        if stop.is_set():
            break


def shutdown(*_):
    stop.set()
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


def save_meta(path, meta):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", required=True, choices=["bike", "car"])
    ap.add_argument("--phone", default="unknown")
    ap.add_argument("--mount", default="unknown")
    ap.add_argument("--notes", default="")
    ap.add_argument("--live", nargs="?", const="auto", default=None,
                    help="also stream to the dashboard (uses ~/rcm/server.txt, or give http://IP:8000)")
    ap.add_argument("--delay", type=int, default=20, help="requested sensor delay in ms (20 = ~50 Hz)")
    ap.add_argument("--gps-wait", type=int, default=90, help="seconds to wait for a GPS fix (0 = do not wait)")
    a = ap.parse_args()

    os.makedirs(DRIVES, exist_ok=True)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    subprocess.run(["termux-wake-lock"])

    sensor = find_accel_sensor()
    drive_id = next_drive_id()
    started = datetime.now()
    stem = "%s_%s_%s" % (drive_id, started.strftime("%Y-%m-%d"), a.vehicle)
    csv_path = os.path.join(DRIVES, stem + ".csv")
    meta_path = os.path.join(DRIVES, drive_id + "_meta.json")
    meta = {"drive_id": drive_id, "vehicle_type": a.vehicle, "phone_model": a.phone,
            "mount_type": a.mount, "notes": a.notes, "sensor": sensor,
            "requested_delay_ms": a.delay, "csv_file": stem + ".csv",
            "started_at": started.isoformat(timespec="seconds"), "status": "recording"}
    save_meta(meta_path, meta)

    threading.Thread(target=gps_worker, daemon=True).start()
    if a.live:
        base = open(os.path.join(BASE, "server.txt")).read().strip() if a.live == "auto" else a.live
        threading.Thread(target=live_worker, args=(base, a.phone), daemon=True).start()
        print("Live streaming to", base)
    if a.gps_wait:
        print("Waiting for GPS fix (up to %d s)..." % a.gps_wait)
        t_end = time.time() + a.gps_wait
        while time.time() < t_end and not stop.is_set():
            with gps_lock:
                got = gps["t"] is not None
                acc = gps["acc"]
            if got:
                print("GPS fix, accuracy %s m" % acc)
                break
            time.sleep(1)
        else:
            if not stop.is_set():
                print("WARNING: no GPS fix yet. Logging anyway; GPS columns stay empty until a fix.")

    print("Drive %s | sensor: %s | file: %s" % (drive_id, sensor, csv_path))
    print("Recording. Press Ctrl+C to stop.")
    p = subprocess.Popen(["termux-sensor", "-s", sensor, "-d", str(a.delay)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    procs.append(p)

    n = 0
    t0 = last_flush = last_sync = last_print = time.time()
    n_print = 0
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for o in json_stream(p):
            now = time.time()
            try:
                ax, ay, az = next(iter(o.values()))["values"][:3]
            except (StopIteration, KeyError, TypeError, ValueError):
                continue
            with gps_lock:
                g = dict(gps)
            if g["t"] is None:
                gcols = ["", "", "", "", "", ""]
            else:
                gcols = [g["lat"], g["lon"], round(g["speed"], 2), g["acc"], g["bearing"],
                         int((now - g["t"]) * 1000)]
            w.writerow([int(now * 1000), round(ax, 4), round(ay, 4), round(az, 4)] + gcols)
            if a.live and len(live_buf) < 3000:
                with live_lock:
                    live_buf.append([int(now * 1000), round(ax, 3), round(ay, 3), round(az, 3)]
                                    + [None if c == "" else c for c in gcols])
            n += 1
            if now - last_flush >= 1:
                f.flush(); last_flush = now
            if now - last_sync >= 10:
                os.fsync(f.fileno()); last_sync = now
            if now - last_print >= 5:
                hz = (n - n_print) / (now - last_print)
                age = "-" if g["t"] is None else "%.1fs" % (now - g["t"])
                spd = "-" if g["t"] is None else "%.0f" % g["speed"]
                print("%s rows=%d  %.1f Hz  gps_age=%s  speed=%s km/h" %
                      (datetime.now().strftime("%H:%M:%S"), n, hz, age, spd))
                last_print, n_print = now, n
            if stop.is_set():
                break
        f.flush(); os.fsync(f.fileno())

    t1 = time.time()
    shutdown()
    subprocess.run(["termux-wake-unlock"])
    meta.update(status="finished", ended_at=datetime.now().isoformat(timespec="seconds"),
                rows=n, duration_s=round(t1 - t0, 1), avg_hz=round(n / max(t1 - t0, 1e-9), 1))
    save_meta(meta_path, meta)
    print("Done. %d rows, %.1f Hz average. Now run: python ~/rcm/check_drive.py %s" %
          (n, meta["avg_hz"], drive_id))


if __name__ == "__main__":
    main()
