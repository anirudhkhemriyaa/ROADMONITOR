package com.roadmonitor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import com.roadmonitor.pipeline.Calibrator
import com.roadmonitor.pipeline.RawSample
import com.roadmonitor.pipeline.TripMetadata
import com.roadmonitor.pipeline.TripPipeline
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service: logs accelerometer + gyroscope + gravity + GPS to one
 * CSV per drive and runs the on-device pipeline at trip end.
 *
 * Kept from the original version: PARTIAL_WAKE_LOCK, 50 Hz accel, 1 s CSV
 * flush, empty GPS fields on no fix, manual markers, live streaming,
 * storage-fail-safe, auto upload on trip end.
 */
class LoggerService : Service(), SensorEventListener, LocationListener {

    companion object {
        @Volatile var instance: LoggerService? = null
        @Volatile var running = false
        @Volatile var rows = 0L
        @Volatile var hz = 0.0
        @Volatile var effectiveHzAvg = 0.0
        @Volatile var gpsText = "no fix"
        @Volatile var driveId = ""
        @Volatile var tripId = ""
        @Volatile var liveStatus = "-"
        @Volatile var uploadStatus = "-"
        @Volatile var calibratingLeftS = 0.0
        fun mark(label: String) { instance?.writeMark(label) }

        // Legacy columns first (existing dashboard/preview depend on them),
        // new plan section 4.1 columns appended.
        private const val HEADER =
            "timestamp_ms,ax,ay,az,lat,lon,speed_kmh,gps_accuracy_m,bearing_deg,gps_age_ms," +
            "t_ns,t_wall_ms,gx,gy,gz,grav_x,grav_y,grav_z,speed_mps,altitude,speed_acc\n"

        private const val GYRO_CARRY_MAX_MS = 200L
        private const val CALIB_PM = "pm"
    }

    private var sm: SensorManager? = null
    private var lm: LocationManager? = null
    private var wake: PowerManager.WakeLock? = null
    private var thread: HandlerThread? = null
    private var csvOut: BufferedWriter? = null
    private var markOut: BufferedWriter? = null
    private var dir: File? = null
    private var metaFile: File? = null
    private var meta = JSONObject()
    private var epochOffsetMs = 0L
    private var lastLoc: Location? = null
    private val liveBuf = ArrayList<String>()
    private var liveOn = false
    private var device = "phone"
    private var server = ""
    private var t0 = 0L
    private var lastFlush = 0L
    private var lastStatT = 0L
    private var lastStatN = 0L
    private val sb = StringBuilder()

    // calibration
    private var calibRequested = false
    private var calibMode = false
    private val calibSamples = ArrayList<Triple<Double, Double, Double>>()
    private var calibStartElapsed = 0L
    private var calibSaved: Calibrator.Result? = null

    // gyro / gravity carry (typed once per event, reused on accel rows)
    private var lastGyroT: Long = -1
    private var lastGx = 0f; private var lastGy = 0f; private var lastGz = 0f
    private var lastGravT: Long = -1
    private var lastGravX = 0f; private var lastGravY = 0f; private var lastGravZ = 0f

    // trip buffer for the on-device pipeline
    private val tripBuffer = ArrayList<RawSample>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running || intent == null) return START_NOT_STICKY
        val vehicle = intent.getStringExtra("vehicle") ?: "bike"
        device = intent.getStringExtra("phone") ?: Build.MODEL
        val mount = intent.getStringExtra("mount") ?: ""
        server = intent.getStringExtra("server") ?: ""
        liveOn = intent.getBooleanExtra("live", false) && server.isNotBlank()
        calibRequested = intent.getBooleanExtra("calibrate", false)
        if (calibRequested) {
            calibMode = true
            calibStartElapsed = SystemClock.elapsedRealtime()
            calibratingLeftS = Calibrator.DURATION_S
        }
        startForegroundNow()

        val d = Uploader.driveDir(this)
        dir = d
        val id = nextId(d)
        driveId = id
        tripId = com.roadmonitor.pipeline.TripMetadata.newTripId()
        val csv = File(d, "${id}_${SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())}_$vehicle.csv")
        csvOut = BufferedWriter(FileWriter(csv), 1 shl 16).also { it.write(HEADER) }
        metaFile = File(d, "${id}_meta.json")
        meta = JSONObject()
            .put("drive_id", id)
            .put("trip_id", tripId)
            .put("device_id", hashedDeviceId())
            .put("app_version", appVersion())
            .put("model", Build.MODEL)
            .put("android", Build.VERSION.SDK_INT)
            .put("vehicle_type", vehicle)
            .put("mount_type", mount)
            .put("phone_model", device)          // legacy field, kept for old backend
            .put("source", "android_app")
            .put("csv_file", csv.name)
            .put("started_at", iso())
            .put("calibrate_requested", calibRequested)
            .put("status", "recording")
        saveMeta()

        wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rcm:log")
            .also { it.acquire(12 * 60 * 60 * 1000L) }

        // Hardware timestamps (nanoseconds since boot) are converted to epoch (unix) milliseconds.
        epochOffsetMs = System.currentTimeMillis() - SystemClock.elapsedRealtimeNanos() / 1_000_000L
        rows = 0; hz = 0.0; effectiveHzAvg = 0.0; gpsText = "no fix"; liveStatus = "-"
        t0 = SystemClock.elapsedRealtime(); lastFlush = t0; lastStatT = t0; lastStatN = 0
        running = true
        instance = this

        val th = HandlerThread("rcm-sensors").also { it.start() }
        thread = th
        val handler = Handler(th.looper)
        sm = getSystemService(SENSOR_SERVICE) as SensorManager
        val acc = sm!!.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        if (acc == null) { gpsText = "NO ACCELEROMETER"; stopSelf(); return START_NOT_STICKY }
        sm!!.registerListener(this, acc, 20000, handler)   // 20 000 us = 50 Hz requested
        sm!!.getDefaultSensor(Sensor.TYPE_GYROSCOPE)?.let {
            sm!!.registerListener(this, it, 20000, handler)
        }
        sm!!.getDefaultSensor(Sensor.TYPE_GRAVITY)?.let {
            sm!!.registerListener(this, it, 20000, handler)
        }

        lm = getSystemService(LOCATION_SERVICE) as LocationManager
        try {
            lm!!.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, this, th.looper)
        } catch (e: SecurityException) {
            gpsText = "location permission missing"
        }
        if (liveOn) Thread { liveLoop() }.start()
        return START_NOT_STICKY
    }

    override fun onSensorChanged(e: SensorEvent) {
        val w = csvOut ?: return
        when (e.sensor.type) {
            Sensor.TYPE_GYROSCOPE -> {
                lastGx = e.values[0]; lastGy = e.values[1]; lastGz = e.values[2]
                lastGyroT = e.timestamp / 1_000_000L
            }
            Sensor.TYPE_GRAVITY -> {
                lastGravX = e.values[0]; lastGravY = e.values[1]; lastGravZ = e.values[2]
                lastGravT = e.timestamp / 1_000_000L
            }
            Sensor.TYPE_ACCELEROMETER -> onAccel(e)
        }
    }

    private fun onAccel(e: SensorEvent) {
        val w = csvOut ?: return
        if (calibMode) { collectCalib(e); return }

        val ts = epochOffsetMs + e.timestamp / 1_000_000L   // unix ms (legacy column)
        val tNs = e.timestamp                                // raw nanos since boot (plan 4.1)
        val tWallMs = System.currentTimeMillis()
        val l = lastLoc

        val gyroFresh = lastGyroT >= 0 &&
            kotlin.math.abs(e.timestamp / 1_000_000L - lastGyroT) <= GYRO_CARRY_MAX_MS
        val gravFresh = lastGravT >= 0 &&
            kotlin.math.abs(e.timestamp / 1_000_000L - lastGravT) <= GYRO_CARRY_MAX_MS

        // ---- CSV row (legacy columns then the appended plan columns) ----
        sb.setLength(0)
        sb.append(ts).append(',')
        sb.append(r(e.values[0], 10000f)).append(',')
            .append(r(e.values[1], 10000f)).append(',')
            .append(r(e.values[2], 10000f))
        if (l != null) {
            sb.append(',').append(l.latitude).append(',').append(l.longitude).append(',')
            if (l.hasSpeed()) sb.append(r(l.speed * 3.6f, 100f))
            sb.append(',')
            if (l.hasAccuracy()) sb.append(l.accuracy)
            sb.append(',')
            if (l.hasBearing()) sb.append(l.bearing)
            sb.append(',').append((e.timestamp - l.elapsedRealtimeNanos) / 1_000_000L)
        } else {
            sb.append(",,,,,,")
        }
        sb.append(',').append(tNs)
            .append(',').append(tWallMs)
        if (gyroFresh) sb.append(',').append(r(lastGx, 1000f)).append(',')
            .append(r(lastGy, 1000f)).append(',').append(r(lastGz, 1000f))
        else sb.append(",,,")
        if (gravFresh) sb.append(',').append(r(lastGravX, 10000f)).append(',')
            .append(r(lastGravY, 10000f)).append(',').append(r(lastGravZ, 10000f))
        else sb.append(",,,")
        if (l != null && l.hasSpeed()) sb.append(',').append(r(l.speed, 1000f))
        else sb.append(',')
        if (l != null && l.hasAltitude()) sb.append(',').append(l.altitude)
        else sb.append(',')
        if (l != null && l.hasSpeedAccuracy()) sb.append(',').append(r(l.speedAccuracyMetersPerSecond, 100f))
        else sb.append(',')

        try {
            w.write(sb.toString()); w.write("\n")
        } catch (ex: Exception) {
            gpsText = "WRITE ERROR (storage full?)"
        }

        // ---- trip buffer for the on-device pipeline ----
        val speedMps = if (l != null && l.hasSpeed()) l.speed.toDouble() else Double.NaN
        synchronized(tripBuffer) {
            tripBuffer.add(
                RawSample(
                    tMs = ts, tNs = tNs,
                    ax = e.values[0].toDouble(), ay = e.values[1].toDouble(), az = e.values[2].toDouble(),
                    gx = if (gyroFresh) lastGx.toDouble() else 0.0,
                    gy = if (gyroFresh) lastGy.toDouble() else 0.0,
                    gz = if (gyroFresh) lastGz.toDouble() else 0.0,
                    gravX = if (gravFresh) lastGravX.toDouble() else 0.0,
                    gravY = if (gravFresh) lastGravY.toDouble() else 0.0,
                    gravZ = if (gravFresh) lastGravZ.toDouble() else 0.0,
                    speedMps = speedMps,
                    lat = l?.latitude, lon = l?.longitude,
                    gpsAcc = if (l?.hasAccuracy() == true) l.accuracy.toDouble() else null,
                    altitude = if (l?.hasAltitude() == true) l.altitude else null,
                    speedAcc = if (l?.hasSpeedAccuracy() == true) l.speedAccuracyMetersPerSecond.toDouble() else null,
                ),
            )
        }

        if (liveOn) {
            val row = "[" + sb.toString().split(',').joinToString(",") { if (it.isEmpty()) "null" else it } + "]"
            synchronized(liveBuf) { if (liveBuf.size < 3000) liveBuf.add(row) }
        }
        rows = rows + 1
        val now = SystemClock.elapsedRealtime()
        if (now - lastFlush >= 1000) {
            try { w.flush() } catch (ex: Exception) { }
            hz = (rows - lastStatN) * 1000.0 / (now - lastStatT)
            effectiveHzAvg = if (now - t0 > 0) rows * 1000.0 / (now - t0) else 0.0
            lastStatT = now; lastStatN = rows; lastFlush = now
            if (l != null) {
                gpsText = "fix, accuracy " + (if (l.hasAccuracy()) l.accuracy.toInt().toString() else "?") +
                        " m, " + (if (l.hasSpeed()) (l.speed * 3.6f).toInt().toString() else "?") + " km/h"
            }
        }
    }

    /** 30 s stationary calibration branch (plan phase A item 4). */
    private fun collectCalib(e: SensorEvent) {
        calibSamples.add(Triple(e.values[0].toDouble(), e.values[1].toDouble(), e.values[2].toDouble()))
        val left = Calibrator.DURATION_S - (SystemClock.elapsedRealtime() - calibStartElapsed) / 1000.0
        calibratingLeftS = left.coerceAtLeast(0.0)
        gpsText = String.format(Locale.US, "calibrating... %.0f s left, hold still", left)
        if (left <= 0.0) {
            calibMode = false
            val r = Calibrator.compute(calibSamples)
            calibSaved = r
            val prefs = getSharedPreferences("rcm", MODE_PRIVATE)
            if (r != null) {
                prefs.edit().putString("calib_json",
                    "{\"bias_x\":${r.calib.biasX},\"bias_y\":${r.calib.biasY}," +
                        "\"bias_z\":${r.calib.biasZ},\"noise_std\":${r.calib.noiseStd}}"
                ).apply()
                gpsText = "calibrated, noise " + String.format(Locale.US, "%.3f", r.calib.noiseStd)
            } else {
                gpsText = "calibration failed (not enough samples)"
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    override fun onLocationChanged(location: Location) { lastLoc = location }
    @Deprecated("kept for API < 30")
    override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}

    @Synchronized
    private fun writeMark(label: String) {
        if (!running) return
        try {
            if (markOut == null) {
                markOut = BufferedWriter(FileWriter(File(dir, "${driveId}_markers.csv")))
                markOut!!.write("timestamp_ms,label\n")
            }
            markOut!!.write("${System.currentTimeMillis()},$label\n")
            markOut!!.flush()
        } catch (e: Exception) { }
    }

    private fun liveLoop() {
        val url = URL(server.trimEnd('/') + "/api/live")
        while (running) {
            Thread.sleep(1000)
            val batch = synchronized(liveBuf) { val b = ArrayList(liveBuf); liveBuf.clear(); b }
            if (batch.isEmpty()) continue
            try {
                val c = url.openConnection() as HttpURLConnection
                c.requestMethod = "POST"; c.doOutput = true
                c.connectTimeout = 2000; c.readTimeout = 2000
                c.setRequestProperty("Content-Type", "application/json")
                val body = "{\"device\":" + JSONObject.quote(device) + ",\"rows\":[" + batch.joinToString(",") + "]}"
                c.outputStream.use { it.write(body.toByteArray()) }
                liveStatus = if (c.responseCode == 200) "ok" else "HTTP " + c.responseCode
                c.disconnect()
            } catch (e: Exception) {
                liveStatus = "unreachable"   // dropped batch is fine; the CSV file is the real record
            }
        }
    }

    override fun onDestroy() {
        running = false
        try { sm?.unregisterListener(this) } catch (e: Exception) { }
        try { lm?.removeUpdates(this) } catch (e: Exception) { }
        thread?.quitSafely()
        try { thread?.join(2000) } catch (e: Exception) { }
        try { csvOut?.flush(); csvOut?.close() } catch (e: Exception) { }
        try { markOut?.close() } catch (e: Exception) { }
        val secs = (SystemClock.elapsedRealtime() - t0) / 1000.0
        try {
            meta.put("status", "finished").put("ended_at", iso()).put("rows", rows)
                .put("duration_s", Math.round(secs * 10) / 10.0)
                .put("avg_hz", if (secs > 0) Math.round(rows / secs * 10) / 10.0 else 0.0)
                .put("effective_hz", Math.round(effectiveHzAvg * 10) / 10.0)
            calibSaved?.let { c ->
                val cb = JSONObject()
                    .put("bias_x", c.calib.biasX).put("bias_y", c.calib.biasY)
                    .put("bias_z", c.calib.biasZ).put("noise_std", c.calib.noiseStd)
                meta.put("calib", cb)
            } ?: run {
                // fall back to the persisted calibration, if any
                getSharedPreferences("rcm", MODE_PRIVATE).getString("calib_json", null)?.let {
                    try { meta.put("calib", JSONObject(it)) } catch (e: Exception) { }
                }
            }
            saveMeta()
        } catch (e: Exception) { }

        // on-device pipeline: events.jsonl + segments.jsonl next to the CSV
        val d = dir
        var pipelineNote = ""
        if (d != null && tripId.isNotBlank()) {
            val samples = synchronized(tripBuffer) { val copy = tripBuffer.toList(); tripBuffer.clear(); copy }
            val out = TripPipeline.processAndWrite(d, samples, tripId)
            pipelineNote = if (out.error != null) "failed: ${out.error}"
                else "${out.nEvents} events, ${out.nBins} bins"
            try { meta.put("pipeline", pipelineNote) } catch (e: Exception) { }
            saveMeta()
        }

        wake?.let { if (it.isHeld) it.release() }
        instance = null

        // upload: prefer the /api/v1/trips queue (WorkManager), legacy /upload as fallback
        val d2 = dir
        if (d2 != null && rows > 0) {
            Thread { uploadStatus = Uploader.finishTrip(this, d2, tripId) }.start()
        }
    }

    private fun startForegroundNow() {
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(NotificationChannel("rcm", "Recording", NotificationManager.IMPORTANCE_LOW))
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, "rcm")
            .setContentTitle("Road Monitor is recording")
            .setContentText("Logging motion and GPS")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(pi).setOngoing(true).build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        else startForeground(1, n)
    }

    private fun nextId(d: File): String {
        var max = 0
        d.list()?.forEach { name ->
            Regex("^D(\\d+)_").find(name)?.let { max = maxOf(max, it.groupValues[1].toInt()) }
        }
        return "D%03d".format(max + 1)
    }

    private fun r(v: Float, scale: Float): Float = Math.round(v * scale) / scale
    private fun iso(): String = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).format(Date())
    private fun appVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "2.0.0"
    } catch (e: Exception) { "2.0.0" }

    private fun hashedDeviceId(): String {
        val prefs = getSharedPreferences("rcm", MODE_PRIVATE)
        var uuid = prefs.getString("install_uuid", null)
        if (uuid == null) {
            uuid = java.util.UUID.randomUUID().toString()
            prefs.edit().putString("install_uuid", uuid).apply()
        }
        return TripMetadata.hashedDeviceId(uuid)
    }

    private fun saveMeta() {
        val f = metaFile ?: return
        try {
            val tmp = File(f.path + ".tmp")
            tmp.writeText(meta.toString(2))
            tmp.renameTo(f)
        } catch (e: Exception) { }
    }
}
