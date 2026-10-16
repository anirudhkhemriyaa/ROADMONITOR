package com.roadmonitor

import android.content.Context
import org.json.JSONObject
import java.io.DataOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

object Uploader {
    fun driveDir(ctx: Context): File =
        File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "drives").also { it.mkdirs() }

    /** Uploads every finished drive that has no ".uploaded" marker yet. Safe to call repeatedly. */
    fun uploadPending(dir: File, base: String): String {
        val csvs = dir.listFiles { f -> f.name.endsWith(".csv") && !f.name.endsWith("_markers.csv") }
            ?: return "no drives"
        var ok = 0
        var fail = 0
        var lastError = ""
        for (csv in csvs.sortedBy { it.name }) {
            val done = File(dir, csv.name + ".uploaded")
            val id = csv.name.substringBefore("_")
            if (done.exists()) continue
            if (LoggerService.running && LoggerService.driveId == id) continue
            val metaFile = File(dir, "${id}_meta.json")
            val meta = if (metaFile.exists()) JSONObject(metaFile.readText()) else JSONObject()
            val markers = File(dir, "${id}_markers.csv").takeIf { it.exists() }
            try {
                done.writeText(post(base, csv, meta, markers))
                ok++
            } catch (e: Exception) {
                fail++
                lastError = e.message ?: e.javaClass.simpleName
            }
        }
        return if (ok + fail == 0) "nothing to upload"
        else "uploaded $ok, failed $fail" + if (fail > 0) " ($lastError)" else ""
    }

    private fun post(base: String, csv: File, meta: JSONObject, markers: File?): String {
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
        return text
    }
}
