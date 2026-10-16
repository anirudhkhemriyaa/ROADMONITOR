"""Rule-based anomaly detection over the ingested accel/gps signal.

NO ML - the rules and every threshold come from config.yaml (per
vehicle_type). All outputs are labelled "threshold-based, unvalidated".

Rules (vertical acceleration a_vert, see ingest.vertical_accel):
  pothole     : sharp downward dip (<= -dip_abs) followed by a high-G impact
                spike (>= +spike_abs) within the pothole_window_ms range.
  speed_bump  : smooth heave - a broad rise above bump_peak without a sharp
                dip, lasting bump_window_ms (compression-then-release wave).
  rough       : sustained broadband energy - a rolling window of
                rough_window_ms whose fraction of samples above rough_rms
                exceeds rough_overlap.
Gates (all thresholds in config):
  - speed gate: outside (min_kmh, max_kmh) nothing is reported
  - gyro gate:  |gyro_z| beyond gyro_z_rad_s within gyro_shield_ms of a
                candidate = turn/swerve, not a road defect -> dropped
  - refractory: at most one event per refractory_ms of any type

ML plug-in point: keep this module's run() signature and output schema and
swap the internals for a trained classifier - downstream modules
(mapmatch/aggregate/priority/API) don't know or care which produced it.
"""
from .ingest import vertical_accel


def scale_thresholds(cfg, vehicle_type: str) -> dict:
    """Base block scaled per vehicle: heavier vehicles record less vertical
    shake for the same defect (better suspension / higher mass), so their
    magnitude thresholds are raised. Returns the effective threshold set."""
    base = dict(cfg["anomaly"]["base"])
    k = cfg["anomaly"]["vehicle_scale"].get(vehicle_type, 1.0)
    for key in ("dip_abs_ms2", "spike_abs_ms2", "bump_peak_abs_ms2",
                "rough_rms_ms2"):
        base[key] *= k
    return base


def _prefix_rms(ts, vert, win_ms):
    """Rolling RMS over a time window (prefix-sum, O(1) per sample)."""
    n = len(ts)
    sums = [0.0]
    sq = [0.0]
    for i, v in enumerate(vert):
        sums.append((sums[-1] + v) if v is not None else sums[-1])
        sq.append((sq[-1] + v * v) if v is not None else sq[-1])
    import bisect
    rms = [0.0] * n
    lo = 0
    for i, t in enumerate(ts):
        t0 = t - win_ms
        while ts[lo] < t0:
            lo += 1
        cnt = i - lo + 1
        s = sums[i + 1] - sums[lo]
        s2 = sq[i + 1] - sq[lo]
        # population variance: (Σx² − (Σx)²/n) / n  (the outer /n was missing)
        rms[i] = max(0.0, (s2 - s * s / cnt) / cnt) ** 0.5   # clamp float error
    return rms, lo


def run(rows: list, vehicle_type: str = "bike", cfg=None) -> list:
    """rows = parsed CSV dicts (ingest.parse_trip_csv). Returns event dicts:
    {event_type, ts, lat, lon, vehicle_type, features{...}}"""
    cfg = cfg or {}
    th = scale_thresholds(cfg, vehicle_type)
    sample_hz = cfg["anomaly"].get("sample_hz", 50)

    verts = vertical_accel(rows)
    ts, vert, gz, latlon, speed = [], [], [], [], []
    last_ll = (None, None)
    for i, r in enumerate(rows):
        if r.get("timestamp_ms") is None:
            continue
        if r.get("lat") is not None and r.get("lon") is not None:
            last_ll = (r["lat"], r["lon"])
        ts.append(r["timestamp_ms"])
        vert.append(verts[i])
        gz.append(r.get("gz"))
        latlon.append(last_ll)
        kmh = r.get("speed_kmh")
        if kmh is None and r.get("speed_mps") is not None:
            kmh = r["speed_mps"] * 3.6
        speed.append(kmh)

    if sample_hz <= 0:
        sample_hz = 50
    n_half = int(sample_hz * th["rough_window_ms"] / 2000.0)
    import math
    rms_win, _ = _prefix_rms(ts, vert, th["rough_window_ms"])

    def speed_ok(i):
        s = speed[i]
        if s is None:                     # no GPS speed -> allow (GPS-less logs)
            return True
        return (cfg.get("speed", {}).get("min_kmh", 5.0)
                <= s <= cfg.get("speed", {}).get("max_kmh", 120.0))

    def gyro_ok(i, t):
        shield = th["gyro_shield_ms"]
        peak = 0.0
        span = int(shield * sample_hz / 1000.0)
        lo, hi = max(0, i - span), min(len(ts), i + span)
        for j in range(lo, hi):
            if gz[j] is not None:
                peak = max(peak, abs(gz[j]))
        return peak <= th["gyro_z_rad_s"], peak

    events = []
    refr = th["refractory_ms"]
    search_back = int(th["bump_window_ms"][1] * sample_hz / 1000.0)
    last_ts = -10**18
    rough_last = last_ts

    for i in range(1, len(ts)):
        t = ts[i]
        if t - last_ts < refr:
            continue
        v = vert[i]
        if v is None or not speed_ok(i):
            continue
        # --- pothole: dip then impact within the configured window ---
        if v >= th["spike_abs_ms2"]:
            dip_v, dip_i = 0.0, None
            back = int(th["pothole_window_ms"][1] * sample_hz / 1000.0)
            for j in range(max(0, i - back), i):
                if vert[j] is not None and vert[j] < dip_v:
                    dip_v, dip_i = vert[j], j
            if dip_i is not None:
                width = t - ts[dip_i]
                if (th["pothole_window_ms"][0] <= width
                        <= th["pothole_window_ms"][1]):
                    ok, gpk = gyro_ok(i, t)
                    if ok:
                        events.append(_ev("pothole", ts, latlon, i, vehicle_type,
                                          peak=v, dip=dip_v, width_ms=width,
                                          rms=rms_win[i], gyro_peak=gpk))
                        last_ts = t
                        continue
        # --- speed bump: broad heave without a sharp dip ---
        if (th["bump_peak_abs_ms2"] <= v < th["spike_abs_ms2"]):
            rise_i = None
            for j in range(max(0, i - search_back), i):
                if vert[j] is None:
                    continue
                if vert[j] >= v:
                    break                     # this heave already emitted
                if vert[j] <= 0.2 * th["bump_peak_abs_ms2"]:
                    rise_i = j
            if rise_i is not None:
                width = t - ts[rise_i]
                if th["bump_window_ms"][0] <= width <= th["bump_window_ms"][1]:
                    ok, gpk = gyro_ok(i, t)
                    if (ok and gyro_peak_below_bump(
                            vert, gz, rise_i, i, spike_v=th["spike_abs_ms2"])):
                        events.append(_ev("speed_bump", ts, latlon, i,
                                          vehicle_type, peak=v,
                                          width_ms=width, rms=rms_win[i],
                                          gyro_peak=gpk))
                        last_ts = t
                        continue
        # --- rough road: sustained RMS above threshold ---
        if rms_win[i] >= th["rough_rms_ms2"]:
            if t - rough_last >= refr:
                frac = _frac_above(ts, vert, i, th["rough_window_ms"], sample_hz,
                                   th["rough_rms_ms2"])
                if frac >= th["rough_overlap"]:
                    events.append(_ev("rough", ts, latlon, i, vehicle_type,
                                      peak=rms_win[i], rms=rms_win[i]))
                    rough_last = t
    return events


def gyro_peak_below_bump(vert, gz, lo, hi, spike_v):
    """A true bump wave stays below the pothole spike level; if anything in
    the window breaches it, classify as pothole territory instead."""
    for j in range(lo, hi + 1):
        if vert[j] is not None and vert[j] >= spike_v:
            return False
    return True


def _frac_above(ts, vert, i, win_ms, sample_hz, th_v):
    back = int(win_ms * sample_hz / 1000.0)
    lo = max(0, i - back)
    vals = [v for v in vert[lo:i + 1] if v is not None]
    if not vals:
        return 0.0
    return sum(1 for v in vals if abs(v) >= th_v) / len(vals)


def _ev(kind, ts, latlon, i, vehicle_type, **features) -> dict:
    return {
        "event_type": kind,
        "ts": ts[i],
        "lat": latlon[i][0],
        "lon": latlon[i][1],
        "vehicle_type": vehicle_type,
        "method": "threshold-based, unvalidated",
        "features": features,
    }
