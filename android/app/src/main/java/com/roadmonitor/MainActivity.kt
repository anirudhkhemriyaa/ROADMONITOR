package com.roadmonitor

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var server: EditText
    private lateinit var phone: EditText
    private lateinit var vehicleSpinner: Spinner
    private lateinit var mountSpinner: Spinner
    private lateinit var live: CheckBox
    private lateinit var consent: CheckBox
    private lateinit var allowMobile: CheckBox
    private lateinit var tripsText: TextView
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() { refresh(); loadTrips(); ui.postDelayed(this, 1000) }
    }

    companion object {
        private val VEHICLES = listOf("bike", "car", "e_rickshaw", "bus", "other")
        private val MOUNTS = listOf("dashboard", "handlebar", "pocket", "cupholder", "other")
        private const val PREFS = "rcm"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        fun add(v: View) = col.addView(v)
        fun label(t: String) = TextView(this).apply { text = t; setPadding(0, pad / 2, 0, 0) }
        fun btn(t: String, onClick: () -> Unit) = Button(this).apply { text = t; setOnClickListener { onClick() } }
        fun spinnerPref(name: String, values: List<String>, default: String): Spinner {
            val s = Spinner(this)
            s.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, values)
            val saved = prefs.getString(name, default)
            val idx = values.indexOf(saved)
            s.setSelection(if (idx >= 0) idx else 0)
            return s
        }

        add(label("Backend address (laptop IP and port)"))
        server = EditText(this).apply { setText(prefs.getString("server", "http://192.168.1.5:8000")); setSingleLine() }
        add(server)
        add(btn("Test connection") { testConnection() })

        add(label("Phone name"))
        phone = EditText(this).apply { setText(prefs.getString("phone", Build.MODEL)); setSingleLine() }
        add(phone)

        add(label("Vehicle type"))
        vehicleSpinner = spinnerPref("vehicle", VEHICLES, "bike")
        add(vehicleSpinner)

        add(label("Mount type"))
        mountSpinner = spinnerPref("mount", MOUNTS, "dashboard")
        add(mountSpinner)

        live = CheckBox(this).apply { text = "Live stream to dashboard (tests only)"; isChecked = prefs.getBoolean("live", false) }
        add(live)

        allowMobile = CheckBox(this).apply {
            text = "Allow upload over mobile data (default: WiFi only)"
            isChecked = prefs.getBoolean("allow_mobile_data", false)
        }
        add(allowMobile)

        consent = CheckBox(this).apply {
            text = "I consent to contributing anonymized road-surface data (opt out any time)"
            isChecked = prefs.getBoolean("consent", false)
            setOnCheckedChangeListener { _, b -> prefs.edit().putBoolean("consent", b).apply() }
        }
        add(consent)

        add(btn("Calibrate 30 s (hold phone still) then start") { startLogging(withCalib = true) })
        add(btn("START RECORDING") { startLogging(withCalib = false) })
        add(btn("STOP") { stopService(Intent(this, LoggerService::class.java)) })

        status = TextView(this).apply { textSize = 16f; setPadding(0, pad, 0, pad) }
        add(status)

        add(label("Mark an event (while recording)"))
        val marksRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun small(t: String, l: String) = Button(this).apply {
            text = t; setOnClickListener { LoggerService.mark(l) } }
        marksRow.addView(small("Pothole", "pothole"))
        marksRow.addView(small("Breaker", "speed_breaker"))
        marksRow.addView(small("Rough", "rough_road"))
        add(marksRow)
        add(btn("Upload pending drives") { uploadNow() })

        tripsText = TextView(this).apply { textSize = 13f; setPadding(0, pad, 0, 0) }
        add(label("Trips"))
        add(tripsText)

        setContentView(ScrollView(this).apply { addView(col) })
    }

    override fun onResume() { super.onResume(); ui.post(tick) }
    override fun onPause() { super.onPause(); ui.removeCallbacks(tick) }

    private fun startLogging(withCalib: Boolean) {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        if (!prefs.getBoolean("consent_seen", false)) {
            AlertDialog.Builder(this)
                .setTitle("Data contribution")
                .setMessage("Road Monitor records motion (accelerometer/gyroscope) and GPS while you drive. " +
                    "No personal data is ever logged: the device ID is a random hash and the first and last " +
                    "150 m of every trip are removed before upload. Raw data stays on this phone unless you consent to upload.")
                .setPositiveButton("I understand") { _, _ ->
                    prefs.edit().putBoolean("consent_seen", true).apply()
                    startLogging(withCalib)
                }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        if (!consent.isChecked) {
            Toast.makeText(this, "Consent is required to record trips", Toast.LENGTH_LONG).show()
            return
        }
        val need = ArrayList<String>()
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.ACCESS_FINE_LOCATION)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.POST_NOTIFICATIONS)
        if (need.isNotEmpty()) {
            requestPermissions(need.toTypedArray(), 1)
            pendingCalib = withCalib
            return
        }
        doStart(withCalib)
    }

    private var pendingCalib = false

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            doStart(pendingCalib)
        } else {
            Toast.makeText(this, "Location permission is required", Toast.LENGTH_LONG).show()
        }
    }

    private fun doStart(withCalib: Boolean) {
        if (LoggerService.running) return
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        val vehicle = vehicleSpinner.selectedItem.toString()
        val mount = mountSpinner.selectedItem.toString()
        prefs.edit()
            .putString("server", server.text.toString().trim())
            .putString("phone", phone.text.toString())
            .putString("vehicle", vehicle)
            .putString("mount", mount)
            .putBoolean("live", live.isChecked)
            .putBoolean("allow_mobile_data", allowMobile.isChecked)
            .apply()
        startForegroundService(
            Intent(this, LoggerService::class.java)
                .putExtra("vehicle", vehicle)
                .putExtra("phone", phone.text.toString())
                .putExtra("mount", mount)
                .putExtra("server", server.text.toString().trim())
                .putExtra("live", live.isChecked)
                .putExtra("calibrate", withCalib),
        )
    }

    private fun testConnection() {
        val url = server.text.toString().trim().trimEnd('/') + "/ping"
        Thread {
            val msg = try {
                val c = URL(url).openConnection() as HttpURLConnection
                c.connectTimeout = 4000; c.readTimeout = 4000
                val ok = c.responseCode == 200
                c.disconnect()
                if (ok) "Backend reachable" else "Backend answered HTTP ${c.responseCode}"
            } catch (e: Exception) {
                "Cannot reach backend: ${e.message}"
            }
            runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
        }.start()
    }

    private fun uploadNow() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        prefs.edit().putString("server", server.text.toString().trim()).apply()
        LoggerService.uploadStatus = "queueing..."
        Uploader.enqueuePending(this)
    }

    private fun loadTrips() {
        val trips = Uploader.loadTrips(this)
        tripsText.text = if (trips.isEmpty()) "none yet"
        else trips.takeLast(8).joinToString("\n") { t ->
            "- ${t.startedAt.ifBlank { "?" }}  ${t.status}  ${t.note}  ${t.tripId.take(8)}"
        }
    }

    private fun refresh() {
        val s = StringBuilder()
        if (LoggerService.running) {
            s.append("RECORDING ").append(LoggerService.driveId)
                .append("  trip ").append(LoggerService.tripId.take(8)).append('\n')
            if (LoggerService.calibratingLeftS > 0.0)
                s.append("calibrating: ").append("%.0f".format(LoggerService.calibratingLeftS)).append(" s left\n")
            s.append("rows: ").append(LoggerService.rows).append("   ")
                .append(String.format("%.1f", LoggerService.hz)).append(" Hz\n")
                .append("GPS: ").append(LoggerService.gpsText).append('\n')
                .append("live: ").append(LoggerService.liveStatus).append('\n')
        } else {
            s.append("Not recording\n")
        }
        s.append("upload: ").append(LoggerService.uploadStatus)
        status.text = s.toString()
    }
}
