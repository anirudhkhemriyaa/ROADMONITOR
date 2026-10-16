# Road Monitor: Implementation Plan (Phase A applied version)

Scope: no labeling phase. Rule-based / unsupervised only. Raw data is always kept on
the device so ML classifiers can be trained later once labels exist.

## 1. Tech decisions
| Layer | Choice |
|---|---|
| Android | Kotlin, `LoggerService`, WorkManager, Room (optional) |
| Backend | FastAPI, PostgreSQL + PostGIS, Celery + Redis, SQLAlchemy + Alembic |
| Map matching | OSRM (/match) via Docker, OSM extract of Madhya Pradesh |
| Processing | numpy, scipy, pandas, scikit-learn (DBSCAN, KMeans) |
| Dashboard | React/Vite or plain HTML + MapLibre GL |
| Deploy (dev) | docker-compose (api, worker, db, redis, osrm) |

## 2. Repo layout
```
Road-Monitor/
  android/                 # existing app (Phase A target)
  backend/                 # Phase B target
  web/                     # Phase D target
  data/raw/                # legacy Termux CSVs stay as-is
  phone/                   # Termux scripts (unchanged)
  docs/                    # this file
```

## 3. Config defaults (backend/config.yaml will mirror these)
```yaml
sampling: {accel_hz: 50, gps_hz: 1}
filter: {bandpass_low: 0.5, bandpass_high: 20, order: 2, gravity_lp_hz: 0.5}
speed: {min_kmh: 5, max_kmh: 120}
event:
  abs_threshold_ms2: 3.0
  sigma_k: 3.5
  sigma_window_s: 5
  refractory_ms: 300
  pulse_width_ms: {pothole: [40, 200], bump: [200, 600]}
roughness: {bin_m: 10, speed_alpha: 0.7, min_samples_per_bin: 20}
gps: {max_accuracy_m: 25, trim_start_end_m: 150}
mapmatch: {radius_m: 20, min_confidence: 0.5}
aggregate: {recency_tau_days: 60, conf_k: 2.0}
cluster: {eps_m: 8, min_samples: 2, confirm_min_devices: 2, confirm_min_trips: 3}
classes: {method: percentile, cuts: [50, 75, 90]}
```

## 4. Data contracts
### 4.1 On-device CSV
Legacy 10 columns kept first (existing dashboard depends on them), plan columns
appended after:
```
timestamp_ms, ax, ay, az, lat, lon, speed_kmh, gps_accuracy_m, bearing_deg,
gps_age_ms, t_ns, t_wall_ms, gx, gy, gz, grav_x, grav_y, grav_z, speed_mps,
altitude, speed_acc
```
GPS fields stay empty when there is no fix (existing behaviour).
`t_ns` = `SensorEvent.timestamp` (nanos since boot); `timestamp_ms` is the same
event converted to unix ms. Gyro/gravity columns carry the nearest recent
reading from the other sensors if it is within 200 ms of the accel sample,
otherwise they are empty.

### 4.2 Trip metadata JSON
`trip_id` (UUID), `device_id` (sha-256 hash of an install UUID), `app_version`
(2.0.0), model, android version, `vehicle_type`, `mount_type`, `started_at`,
`ended_at`, `calib` {bias_x/y/z, noise_std}, `effective_hz`.

### 4.3 Edge outputs (written next to the CSV at trip end)
- `{tripId}_events.jsonl`: `{ts, lat, lon, peak, p2p, width_ms, jerk, energy,
  speed, gyro_peak, speed_drop, type_hint, dist_m}`
- `{tripId}_segments.jsonl` (per 10 m): `{ts_start, ts_end, lat, lon, dist_m,
  rms, std, max, p95, kurtosis, band_0_5, band_5_15, band_15p, speed_mean, n}`
  Bands (documented assumption): `band_0_5` = 0.5-5 Hz, `band_5_15` = 5-15 Hz,
  `band_15p` = 15+ Hz. `speed_*` and `speed_drop` are m/s.

`dist_m` allows the uploader to apply the privacy trim without re-parsing the CSV.

### 4.4 Upload paths
- New: `POST /api/v1/trips` (gzip JSON, `X-Trip-Id` idempotency header) - exists
  from Phase B; the app falls back automatically when the server lacks it.
- Legacy: `POST /upload` multipart (unchanged, now sends a distance-trimmed CSV).
- `POST /api/live` unchanged.
- Auth (from Phase B): per-device `X-Device-Key`.

## 5. Algorithms (client side, plan 5.1-5.4)
- 5.1 Orientation fix: `g = lowpass(acc, 0.5 Hz)`, `v = g/|g|`,
  `a_vert = dot(acc, v) - |g|`; horizontal magnitude = |a - dot(a,v)v|.
- 5.2 Filtering: resample to uniform 50 Hz by linear interpolation on
  `SensorEvent.timestamp` (gaps > 1.5 s start a new segment), then a 2nd-order
  Butterworth band-pass 0.5-20 Hz applied zero-phase (filtfilt).
- 5.3 Event detection: `|a_vert| > max(3.0, 3.5 * rolling_std(5 s))` and speed
  in (5, 120) km/h; 300 ms refractory; features: peak, p2p, half-max width,
  jerk, energy, speed drop (1 s before vs after), gyro peak; reject when
  `|gyro_z| > 0.5 rad/s`; type hint: pothole range 40-200 ms + dip-then-impact,
  bump range 200-600 ms, else rough.
- 5.4 Roughness: 10 m bins via trapezoidal speed integration; rms, std, max,
  p95, excess kurtosis, 3 FFT band energies (Hann window, zero-padded radix-2),
  mean speed, sample count; bins with < 20 samples are skipped.

## 6. Phases
- **Phase A (Android)** - this repo session: sensors, CSV schema, trip metadata,
  calibration, on-device pipeline, privacy trim, WorkManager uploads, consent,
  trips screen, unit tests.
- **Phase B (Backend core)**: FastAPI package + PostGIS + Celery + OSRM.
- **Phase C (Pipeline quality)**: mapmatch, aggregation, clustering,
  classification, priority, simulation + reports.
- **Phase D (Dashboard)**: map/MapLibre, filters, segment detail, ranking.
- **Phase E (Validation)**: repeatability / cross-device / speed scripts.

## 8. Notes
Raw CSV stays on the device (toggle anywhere); labelling + ML comes later.
Controlled validation first: 2-3 phones, one road, same vehicle type.
