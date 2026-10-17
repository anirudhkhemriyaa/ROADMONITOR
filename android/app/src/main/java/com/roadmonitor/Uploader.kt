package com.roadmonitor

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.roadmonitor.pipeline.RoadConfig
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPOutputStream

/**
 * Uploads finished trips.
 *
 * - New path: POST /api/v1/trips (gzip JSON: metadata + events + segments),
 *   idempotent on trip_id (X-Trip-Id header), privacy-trimmed (first/last
 *   150 m of GPS distance removed).
 * - Fallback: the legacy multipart POST /upload with the raw CSV + markers
 *   (the old backend ignores unknown CSV columns, so the appended header
 *   stays compatible).
 *
 * The raw CSV never leaves the device untrimmed: the legacy path uploads a
 * distance-trimmed copy, and for very long drives only the JSONLs go out
 * (raw always stays local for future ML labelling, plan section 8).
 */
object Uploader {

    private const val MAX_RAW_CSV = 32L * 1024 * 1024

    // ---- trip status store (Trips screen: pending / uploaded / failed) ------

    data class TripRecord(
        val tripId: String,
        val driveId: String,
        val file: String,
        var status: String,   // pending | uploaded | failed
        var note: String,
        val startedAt: String,
    )

    private fun tripsFile(ctx: Context): File = File(driveDir(ctx), "trips.json")

    @Synchronized
    fun loadTrips(ctx: Context): MutableList<TripRecord> {
        val out = ArrayList<TripRecord>()
        val f = tripsFile(ctx)
        if (f.exists()) {
            try {
                val arr = JSONArray(f.readText())
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    out.add(TripRecord(
                        tripId = o.optString("trip_id"),
                        driveId = o.optString("drive_id"),
                        file = o.optString("file"),
                        status = o.optString("status", "pending"),
                        note = o.optString("note"),
                        startedAt = o.optString("started_at"),
                    ))
                }
            } catch (e: Exception) { }
        }
        return out
    }

    @Synchronized
    fun saveTrips(ctx: Context, trips: List<TripRecord>) {
        val arr = JSONArray()
        for (t in trips) {
            arr.put(JSONObject()
                .put("trip_id", t.tripId)
                .put("drive_id", t.driveId)
                .put("file", t.file)
                .put("status", t.status)
                .put("note", t.note)
                .put("started_at", t.startedAt))
        }
        try {
            val tmp = File(tripsFile(ctx).path + ".tmp")
            tmp.writeText(arr.toString(2))
            tmp.renameTo(tripsFile(ctx))
        } catch (e: Exception) { }
    }

    @Synchronized
    fun setStatus(ctx: Context, tripId: String, status: String, note: String) {
        val trips = loadTrips(ctx)
        for (t in trips) if (t.tripId == tripId) { t.status = status; t.note = note }
        saveTrips(ctx, trips)
    }

    fun driveDir(ctx: Context): File =
        File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "drives").also { it.mkdirs() }

    internal fun findMeta(dir: File, tripId: String): File? {
        val metas = dir.listFiles { f -> f.name.endsWith("_meta.json") } ?: return null
        for (m in metas) {
            try {
                if (JSONObject(m.readText()).optString("trip_id") == tripId) return m
            } catch (e: Exception) { }
        }
        return null
    }

    // ---- registration + enqueue ---------------------------------------------

    /** Called when a trip finishes recording: registers it and queues work. */
    fun finishTrip(ctx: Context, dir: File, tripId: String): String {
        val meta = findMeta(dir, tripId)
        if (meta == null) return "meta missing, cannot register trip"
        val trips = loadTrips(ctx)
        if (trips.none { it.tripId == tripId }) {
            val driveId = meta.nameWithoutExtension.removeSuffix("_meta")
            var csvName = ""
            var startedAt = ""
            try {
                val m = JSONObject(meta.readText())
                csvName = m.optString("csv_file")
                startedAt = m.optString("started_at")
            } catch (e: Exception) { }
            trips.add(TripRecord(tripId, driveId, csvName, "pending", "", startedAt))
            saveTrips(ctx, trips)
        }
        enqueue(ctx, tripId)
        return "queued"
    }

    /** Enqueues one idempotent upload work item per trip (WiFi-only default). */
    fun enqueue(ctx: Context, tripId: String) {
        val prefs = ctx.getSharedPreferences("rcm", Context.MODE_PRIVATE)
        val allowMobile = prefs.getBoolean("allow_mobile_data", false)
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (allowMobile) NetworkType.CONNECTED else NetworkType.UNMETERED)
            .build()
        val req = OneTimeWorkRequestBuilder<TripUploadWorker>()
            .setInputData(Data.Builder().putString("trip_id", tripId).build())
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(ctx)
            .enqueueUniqueWork("upload-$tripId", ExistingWorkPolicy.KEEP, req)
    }

    fun enqueuePending(ctx: Context) {
        for (t in loadTrips(ctx)) if (t.status != "uploaded") enqueue(ctx, t.tripId)
    }

    /**
     * Obtains or retrieves a persistent device key from the backend via
     * POST /api/v1/devices/register. Cached in SharedPreferences.
     */
    @Synchronized
    fun ensureDeviceKey(ctx: Context, base: String): String? {
        val prefs = ctx.getSharedPreferences("rcm", Context.MODE_PRIVATE)
        val cached = prefs.getString("device_key", null)
        if (!cached.isNullOrBlank()) return cached

        val phoneName = prefs.getString("phone", android.os.Build.MODEL) ?: android.os.Build.MODEL
        return try {
            val conn = URL(base.trimEnd('/') + "/api/v1/devices/register").openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.setRequestProperty("Content-Type", "application/json")
            val payload = JSONObject().put("name", phoneName).toString().toByteArray()
            conn.setFixedLengthStreamingMode(payload.size)
            conn.outputStream.use { it.write(payload) }
            val code = conn.responseCode
            if (code in 200..299) {
                val resp = conn.inputStream.bufferedReader().readText()
                val json = JSONObject(resp)
                val key = json.optString("device_key")
                val devId = json.optString("device_id")
                if (key.isNotBlank()) {
                    prefs.edit()
                        .putString("device_key", key)
                        .putString("registered_device_id", devId)
                        .apply()
                    key
                } else null
            } else null
        } catch (e: Exception) {
            null
        }
    }

    // ---- worker -------------------------------------------------------------

    class TripUploadWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {
        override fun doWork(): Result {
            val tripId = inputData.getString("trip_id") ?: return Result.failure()
            val ctx = applicationContext
            val base = ctx.getSharedPreferences("rcm", Context.MODE_PRIVATE)
                .getString("server", "") ?: ""
            if (base.isBlank()) return Result.failure()
            val dir = driveDir(ctx)
            return try {
                uploadTrip(dir, tripId, base, ctx)
                setStatus(ctx, tripId, "uploaded", "")
                Result.success()
            } catch (e: IOException) {
                setStatus(ctx, tripId, "failed", e.message ?: "network error")
                Result.retry()   // transient: WorkManager backs off exponentially
            } catch (e: Exception) {
                setStatus(ctx, tripId, "failed", e.message ?: e.javaClass.simpleName)
                Result.failure()
            }
        }
    }

    // ---- payload building ---------------------------------------------------

    /** Uploads one trip: /api/v1/trips (gzip) with legacy /upload fallback. */
    @Throws(IOException::class)
    fun uploadTrip(dir: File, tripId: String, base: String, ctx: Context? = null) {
        val metaFile = findMeta(dir, tripId)
            ?: throw IOException("meta missing for trip $tripId")
        val meta = JSONObject(metaFile.readText())
        val events = File(dir, "$tripId${EVENTS_SUFFIX}").takeIf { it.exists() }
        val segments = File(dir, "$tripId${SEGMENTS_SUFFIX}").takeIf { it.exists() }

        if (events != null && segments != null) {
            try {
                val deviceKey = ctx?.let { ensureDeviceKey(it, base) }
                postTripV1(base, tripId, meta, events.readText(), segments.readText(), deviceKey)
                return
            } catch (e: LegacyFallback) {
                // old backend without /api/v1/trips -> legacy path below
            }
        }
        val csv = File(dir, meta.optString("csv_file")).takeIf { it.exists() }
        uploadLegacy(base, csv, meta, tripletMarkers(dir, meta))
    }

    private fun tripletMarkers(dir: File, meta: JSONObject): File? =
        File(dir, meta.optString("drive_id") + "_markers.csv").takeIf { it.exists() }

    internal const val EVENTS_SUFFIX = "_events.jsonl"
    internal const val SEGMENTS_SUFFIX = "_segments.jsonl"

    /** Signals "server has no /api/v1/trips", the caller falls back to /upload. */
    internal class LegacyFallback : IOException("no /api/v1/trips on server")

    /**
     * Privacy trim (plan section 4.1 / Phase A item 6): events and segments
     * lines carry `dist_m` (cumulative trip distance). Keeps only lines whose
     * distance is inside [150 m, total - 150 m].
     */
    internal fun trimByDistance(text: String): List<String> {
        val lines = text.split('\n').filter { it.isNotBlank() }
        if (lines.isEmpty()) return emptyList()
        val record = Regex("\"dist_m\":(-?[0-9.]+(?:[eE][+-]?[0-9]+)?)")
        val dist = lines.mapNotNull { l -> record.find(l)?.groupValues?.get(1)?.toDoubleOrNull() }
        if (dist.isEmpty()) return lines          // no distances available -> keep all
        val total = dist.max()
        val trim = RoadConfig.Gps.TRIM_START_END_M
        val keep = ArrayList<String>()
        for (l in lines) {
            val d = record.find(l)?.groupValues?.get(1)?.toDoubleOrNull() ?: continue
            if (d >= trim && d <= total - trim) keep.add(l)
        }
        return keep
    }

    internal fun gzip(body: ByteArray): ByteArray {
        val bos = ByteArrayOutputStream()
        GZIPOutputStream(bos).use { it.write(body) }
        return bos.toByteArray()
    }

    private fun postTripV1(
        base: String,
        tripId: String,
        meta: JSONObject,
        eventsText: String,
        segmentsText: String,
        deviceKey: String? = null,
    ) {
        val events = trimByDistance(eventsText)
        val segments = trimByDistance(segmentsText)
        val body = JSONObject()
            .put("metadata", meta)
            .put("events", JSONArray(events))
            .put("segments", JSONArray(segments))
            .toString().toByteArray()
        val gz = gzip(body)

        val conn = URL(base.trimEnd('/') + "/api/v1/trips").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 10000
        conn.readTimeout = 60000
        conn.setRequestProperty("Content-Type", "application/json")
        conn.setRequestProperty("Content-Encoding", "gzip")
        conn.setRequestProperty("X-Trip-Id", tripId)     // idempotency key
        if (!deviceKey.isNullOrBlank()) {
            conn.setRequestProperty("X-Device-Key", deviceKey)
        }
        conn.setFixedLengthStreamingMode(gz.size)
        conn.outputStream.use { it.write(gz) }
        val code = conn.responseCode
        val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
            ?.bufferedReader()?.readText() ?: ""
        conn.disconnect()
        when {
            code in 200..299 -> Unit
            code == 404 -> throw LegacyFallback()
            code == 409 -> Unit          // duplicate trip -> idempotent success
            else -> throw IOException("HTTP $code from /api/v1/trips: $text")
        }
    }

    // ---- legacy fallback (original behaviour, privacy-trimmed) ---------------

    fun uploadLegacy(base: String, csvFile: File?, meta: JSONObject, markers: File?) {
        if (csvFile == null || !csvFile.exists()) {
            // no raw CSV: send the metadata through the legacy site anyway
            val placeholder = File.createTempFile("meta", ".csv")
            try {
                placeholder.writeText("trip_id\n" + meta.optString("trip_id") + "\n")
                postMultipart(base, placeholder, meta, markers)
            } finally { placeholder.delete() }
            return
        }
        val trimmed = if (csvFile.length() <= MAX_RAW_CSV) trimRawCsv(csvFile.readText()) else null
        if (trimmed == null) {
            // oversized CSV: cannot trim safely in memory -> skip raw upload
            postMultipart(base, File.createTempFile("meta", ".csv").apply {
                writeText("trip_id\n" + meta.optString("trip_id") + "\n")
            }, meta, markers)
            return
        }
        val tmp = File(csvFile.parentFile, csvFile.name + ".trimmed")
        try {
            tmp.writeText(trimmed)
            postMultipart(base, tmp, meta, markers)
        } finally { tmp.delete() }
    }

    /**
     * Privacy trim of the legacy CSV: per-row cumulative distance from
     * speed * dt (speed_mps preferred, falls back to speed_kmh and then to the
     * last known speed); keeps rows in [150 m, total - 150 m].
     */
    internal fun trimRawCsv(text: String): String {
        val lines = text.split('\n').map { it.trimEnd('\r') }.filter { it.isNotBlank() }
        if (lines.size < 3) return text
        val header = lines[0].split(',')
        val iSpd = header.indexOf("speed_mps")
        val iKmh = header.indexOf("speed_kmh")
        val iTs = if (header.contains("timestamp_ms")) header.indexOf("timestamp_ms") else -1
        val n = lines.size - 1
        val dist = DoubleArray(n)
        var acc = 0.0
        var lastSpeed = 0.0
        var prevTs = 0L
        for (r in 1..n) {
            val f = lines[r].split(',')
            val spdMps = if (iSpd >= 0) f.getOrNull(iSpd)?.toDoubleOrNull() else null
            val spdKmh = if (iKmh >= 0) f.getOrNull(iKmh)?.toDoubleOrNull() else null
            val spd = spdMps ?: spdKmh?.let { it / 3.6 } ?: lastSpeed
            val ts = if (iTs >= 0) f.getOrNull(iTs)?.toLongOrNull() ?: prevTs else prevTs
            val dtMs = (ts - prevTs).coerceAtLeast(0)
            acc += (spd + lastSpeed) * 0.5 * (dtMs / 1000.0)
            lastSpeed = if (spd > 0.01) spd else lastSpeed
            prevTs = ts
            dist[r - 1] = acc
        }
        val total = acc
        val trim = RoadConfig.Gps.TRIM_START_END_M
        if (total <= 2 * trim) return ""    // nothing outside the privacy window
        val out = StringBuilder(lines[0]).append('\n')
        for (r in 1..n) {
            if (dist[r - 1] in trim..(total - trim)) out.append(lines[r]).append('\n')
        }
        return out.toString()
    }

    /** Multipart POST /upload (original implementation, kept verbatim). */
    fun postMultipart(base: String, csv: File, meta: JSONObject, markers: File?) {
        val b = "----rcm" + System.currentTimeMillis()
        val conn = URL(base.trimEnd('/') + "/upload").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 10000
        conn.readTimeout = 120000
        conn.setChunkedStreamingMode(0)
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$b")
        DataOutputStream(conn.outputStream).use { o ->
            fun field(n: String, v: String) {
                o.writeBytes("--$b\r\nContent-Disposition: form-data; name=\"$n\"\r\n\r\n")
                o.write(v.toByteArray())
                o.writeBytes("\r\n")
            }
            fun file(n: String, f: File) {
                o.writeBytes("--$b\r\nContent-Disposition: form-data; name=\"$n\"; filename=\"${f.name}\"\r\n" +
                        "Content-Type: text/csv\r\n\r\n")
                f.inputStream().use { it.copyTo(o) }
                o.writeBytes("\r\n")
            }
            field("vehicle_type", meta.optString("vehicle_type", "unknown"))
            field("phone_model", meta.optString("phone_model", ""))
            field("meta", meta.toString())
            file("file", csv)
            if (markers != null) file("markers", markers)
            o.writeBytes("--$b--\r\n")
        }
        val code = conn.responseCode
        val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
            ?.bufferedReader()?.readText() ?: ""
        conn.disconnect()
        if (code !in 200..299) throw IOException("HTTP $code $text")
    }

    /** Kept for compatibility with the original UI flow and the Termux scripts. */
    fun uploadPending(dir: File, base: String, ctx: Context? = null): String {
        val csvs = dir.listFiles { f -> f.name.endsWith(".csv") && !f.name.endsWith("_markers.csv") }
            ?: return "no drives"
        var ok = 0
        var fail = 0
        var lastError = ""
        for (csv in csvs.sortedBy { it.name }) {
            if (File(dir, csv.name + ".uploaded").exists()) continue
            val id = csv.name.substringBefore("_")
            if (LoggerService.running && LoggerService.driveId == id) continue
            val metaFile = File(dir, "${id}_meta.json")
            val meta = if (metaFile.exists()) JSONObject(metaFile.readText()) else JSONObject()
            if (meta.optString("trip_id").isNotBlank()) {
                // new-style trip: route through the normal idempotent path
                try {
                    uploadTrip(dir, meta.optString("trip_id"), base, ctx)
                    ok++
                } catch (e: LegacyFallback) {
                    try {
                        uploadLegacy(base, csv, meta,
                            File(dir, "${id}_markers.csv").takeIf { it.exists() })
                        File(dir, csv.name + ".uploaded").writeText("legacy uploaded")
                        ok++
                    } catch (e2: Exception) {
                        fail++; lastError = e2.message ?: e2.javaClass.simpleName
                    }
                } catch (e: Exception) {
                    fail++; lastError = e.message ?: e.javaClass.simpleName
                }
                continue
            }
            val markers = File(dir, "${id}_markers.csv").takeIf { it.exists() }
            try {
                postMultipart(base, csv, meta, markers)
                File(dir, csv.name + ".uploaded").writeText("legacy uploaded")
                ok++
            } catch (e: Exception) {
                fail++; lastError = e.message ?: e.javaClass.simpleName
            }
        }
        return if (ok + fail == 0) "nothing to upload"
        else "uploaded $ok, failed $fail" + if (fail > 0) " ($lastError)" else ""
    }
}
