"""Severity score (0..1) for a detected event.

severity = w_p95 * min(1, p95_v / ref_p95(vt)) + w_rms * min(1, rms / ref_rms(vt))

- p95_v / rms are the vertical-acceleration statistics around the event
  (from the detect features / the surrounding 10 m window).
- The normalization anchors ref_p95/ref_rms come from config per
  vehicle_type: heavier vehicles record lower magnitudes for the same
  defect, so raw values must be compared against vehicle-specific anchors
  before they are comparable across the fleet.
- The mix weights are the AHP-tunable site policy (config severity.weights).
- Output carries method="threshold-based, unvalidated" - no accuracy claim
  is made until labelled data arrives.
"""
import json

METHOD_NOTE = "threshold-based, unvalidated"


def event_severity(features: dict, vehicle_type: str, cfg) -> float:
    sev = cfg["severity"]
    refs = sev["refs"][vehicle_type]
    w = sev["weights"]
    p95 = abs(features.get("p95") or features.get("rms") or 0.0)
    rms = abs(features.get("rms") or 0.0)
    t_p95 = min(1.0, p95 / refs["p95"])
    t_rms = min(1.0, rms / refs["rms"])
    return round(w["p95"] * t_p95 + w["rms"] * t_rms, 4)


def enrich(events: list, cfg) -> list:
    """Adds severity + method to every detection dict (detect.run output)."""
    out = []
    for e in events:
        s = event_severity(e.get("features") or {}, e.get("vehicle_type", "bike"),
                           cfg)
        out.append(dict(e, severity=s, method=METHOD_NOTE))
    return out


def run(rows: list, vehicle_type: str = "bike", cfg=None) -> list:
    """ML plug-in contract: same signature family as detect.run; here it
    just post-processes detect output. A trained model can emit its own
    severity instead - the schema (severity, method) stays the same."""
    cfg = cfg or {}
    from .detect import run as detect_run
    return enrich(detect_run(rows, vehicle_type, cfg), cfg)


def features_json(features: dict) -> str:
    return json.dumps(features)
