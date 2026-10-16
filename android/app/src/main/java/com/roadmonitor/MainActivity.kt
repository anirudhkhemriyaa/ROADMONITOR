package com.roadmonitor

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var server: EditText
    private lateinit var phone: EditText
    private lateinit var mount: EditText
    private lateinit var car: RadioButton
    private lateinit var live: CheckBox
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() { refresh(); ui.postDelayed(this, 1000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("rcm", MODE_PRIVATE)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        fun add(v: View) = col.addView(v)
        fun label(t: String) = TextView(this).apply { text = t; setPadding(0, pad / 2, 0, 0) }
        fun btn(t: String, onClick: () -> Unit) = Button(this).apply { text = t; setOnClickListener { onClick() } }

        add(label("Backend address (laptop IP and port)"))
        server = EditText(this).apply { setText(prefs.getString("server", "http://192.168.1.5:8000")); setSingleLine() }
        add(server)
        add(btn("Test connection") { testConnection() })
        add(label("Phone name"))
        phone = EditText(this).apply { setText(prefs.getString("phone", Build.MODEL)); setSingleLine() }
        add(phone)
        add(label("Mount (e.g. handlebar clamp, dashboard)"))
        mount = EditText(this).apply { setText(prefs.getString("mount", "")); setSingleLine() }
        add(mount)
        val bike = RadioButton(this).apply { text = "Bike"; id = 101 }
        car = RadioButton(this).apply { text = "Car"; id = 102 }
        val grp = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL; addView(bike); addView(car) }
        grp.check(if (prefs.getString("vehicle", "bike") == "car") 102 else 101)
        add(grp)
        live = CheckBox(this).apply { text = "Live stream to dashboard (tests only)"; isChecked = prefs.getBoolean("live", false) }
        add(live)
        add(btn("1. Allow background use (battery)") {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        })
        add(btn("START RECORDING") { startLogging() })
        add(btn("STOP") { stopService(Intent(this, LoggerService::class.java)) })
        status = TextView(this).apply { textSize = 16f; setPadding(0, pad, 0, pad) }
        add(status)
        add(label("Mark an event (while recording)"))
        add(btn("Pothole") { LoggerService.mark("pothole") })
        add(btn("Speed breaker") { LoggerService.mark("speed_breaker") })
        add(btn("Rough road") { LoggerService.mark("rough_road") })
        add(btn("Upload pending drives") { uploadNow() })
        setContentView(ScrollView(this).apply { addView(col) })
    }

    override fun onResume() { super.onResume(); ui.post(tick) }
    override fun onPause() { super.onPause(); ui.removeCallbacks(tick) }

    private fun startLogging() {
        val need = ArrayList<String>()
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.ACCESS_FINE_LOCATION)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.POST_NOTIFICATIONS)
        if (need.isNotEmpty()) { requestPermissions(need.toTypedArray(), 1); return }
        doStart()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) doStart()
        else Toast.makeText(this, "Location permission is required", Toast.LENGTH_LONG).show()
    }

    private fun doStart() {
        if (LoggerService.running) return
        val vehicle = if (car.isChecked) "car" else "bike"
        getSharedPreferences("rcm", MODE_PRIVATE).edit()
            .putString("server", server.text.toString().trim()).putString("phone", phone.text.toString())
            .putString("mount", mount.text.toString()).putString("vehicle", vehicle)
            .putBoolean("live", live.isChecked).apply()
        startForegroundService(Intent(this, LoggerService::class.java)
            .putExtra("vehicle", vehicle).putExtra("phone", phone.text.toString())
            .putExtra("mount", mount.text.toString()).putExtra("server", server.text.toString().trim())
            .putExtra("live", live.isChecked))
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
        val base = server.text.toString().trim()
        val dir = Uploader.driveDir(this)
        LoggerService.uploadStatus = "uploading..."
        Thread { LoggerService.uploadStatus = Uploader.uploadPending(dir, base) }.start()
    }

    private fun refresh() {
        val s = StringBuilder()
        if (LoggerService.running) {
            s.append("RECORDING ").append(LoggerService.driveId).append('\n')
                .append("rows: ").append(LoggerService.rows).append("   ")
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
