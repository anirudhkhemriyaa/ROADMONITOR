"""Road Anomaly Priority Pipeline (server-side extension).

Modules:
  schema.py    - additive tables (SQLite now; PostGIS-ready DDL mirror)
  ingest.py    - decode gzip trip CSV, per-10m windows, store
  detect.py    - rule-based pothole / speed bump / rough-road detection
  severity.py  - 0..1 severity normalised per vehicle_type
  roadnet.py   - OSMnx road network + POIs, stable segment ids   (tranche 2)
  mapmatch.py  - GPS -> road segment_id snapping                (tranche 2)
  aggregate.py - per-segment stats, >=3-trip confirmation       (tranche 2)
  priority.py  - weighted repair-priority formula               (tranche 2)

ML plug-in point: detect.py + severity.py both expose the same
`run_<name>(rows, vehicle_type, cfg) -> [dict]` contract. A trained model
later replaces their internals only; input rows (parsed CSV dicts) and
output dicts (event/severity schema) stay identical, so
ingest/mapmatch/aggregate/priority need no changes. Everything Anomaly-
side is labelled "threshold-based, unvalidated" until label collection
starts; no accuracy claims are made anywhere.
"""
