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
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Foreground service: logs accelerometer + GPS to one CSV per drive (same format as the Termux logger). */
class LoggerService : Service(), SensorEventListener, LocationListener {

    companion object {
        @Volatile var instance: LoggerService? = null
        @Volatile var running = false
        @Volatile var rows = 0L
        @Volatile var hz = 0.0
        @Volatile var gpsText = "no fix"
        @Volatile var driveId = ""
        @Volatile var liveStatus = "-"
        @Volatile var uploadStatus = "-"
        fun mark(label: String) { instance?.writeMark(label) }
        private const val HEADER =
            "timestamp_ms,ax,ay,az,lat,lon,speed_kmh,gps_accuracy_m,bearing_deg,gps_age_ms\n"
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

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running || intent == null) return START_NOT_STICKY
        val vehicle = intent.getStringExtra("vehicle") ?: "bike"
        device = intent.getStringExtra("phone") ?: Build.MODEL
        val mount = intent.getStringExtra("mount") ?: ""
        server = intent.getStringExtra("server") ?: ""
        liveOn = intent.getBooleanExtra("live", false) && server.isNotBlank()
        startForegroundNow()

        val d = Uploader.driveDir(this)
        dir = d
        val id = nextId(d)
        driveId = id
        val csv = File(d, "${id}_${SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())}_$vehicle.csv")
        csvOut = BufferedWriter(FileWriter(csv), 1 shl 16).also { it.write(HEADER) }
        metaFile = File(d, "${id}_meta.json")
        meta = JSONObject()
            .put("drive_id", id).put("vehicle_type", vehicle).put("phone_model", device)
            .put("mount_type", mount).put("source", "android_app").put("csv_file", csv.name)
            .put("started_at", iso()).put("status", "recording")
        saveMeta()

        wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rcm:log")
            .also { it.acquire(12 * 60 * 60 * 1000L) }

        // Hardware timestamps (nanoseconds since boot) are converted to epoch milliseconds.
        epochOffsetMs = System.currentTimeMillis() - SystemClock.elapsedRealtimeNanos() / 1_000_000L
        rows = 0; hz = 0.0; gpsText = "no fix"; liveStatus = "-"
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
        val ts = epochOffsetMs + e.timestamp / 1_000_000L
        val l = lastLoc
        sb.setLength(0)
        sb.append(ts).append(',').append(r(e.values[0], 10000f)).append(',')
            .append(r(e.values[1], 10000f)).append(',').append(r(e.values[2], 10000f))
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
        try {
            w.write(sb.toString()); w.write("\n")
        } catch (ex: Exception) {
            gpsText = "WRITE ERROR (storage full?)"
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
            lastStatT = now; lastStatN = rows; lastFlush = now
            if (l != null) {
                gpsText = "fix, accuracy " + (if (l.hasAccuracy()) l.accuracy.toInt().toString() else "?") +
                        " m, " + (if (l.hasSpeed()) (l.speed * 3.6f).toInt().toString() else "?") + " km/h"
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
            saveMeta()
        } catch (e: Exception) { }
        wake?.let { if (it.isHeld) it.release() }
        instance = null
        val d = dir
        if (d != null && server.isNotBlank() && rows > 0) {
            val s = server
            Thread { uploadStatus = Uploader.uploadPending(d, s) }.start()   // auto-upload when a drive ends
        }
        super.onDestroy()
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
    private fun saveMeta() {
        val f = metaFile ?: return
        try {
            val tmp = File(f.path + ".tmp")
            tmp.writeText(meta.toString(2))
            tmp.renameTo(f)
        } catch (e: Exception) { }
    }
}
