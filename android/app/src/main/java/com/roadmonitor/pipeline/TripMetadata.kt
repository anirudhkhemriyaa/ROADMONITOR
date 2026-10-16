package com.roadmonitor.pipeline

import java.security.MessageDigest
import java.util.UUID
import kotlin.math.sqrt

/**
 * Trip metadata (plan section 4.2) and the 30 s stationary calibration
 * (plan section 6 / Phase A item 4).
 */
object TripMetadata {

    fun newTripId(): String = UUID.randomUUID().toString()

    /**
     * Stable per-install, non-reversible device hash:
     * sha-256(install UUID), hex. Never logs anything personal.
     */
    fun hashedDeviceId(installUuid: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(installUuid.toByteArray())
            .joinToString("") { "%02x".format(it) }

    data class TripInfo(
        val tripId: String,
        val deviceId: String,
        val appVersion: String,
        val model: String,
        val android: Int,
        val vehicleType: String,
        val mountType: String,
        val startedAtIso: String,
        val endedAtIso: String,
        val calib: Calibration?,
        val effectiveHz: Double,
        val rows: Long,
        val durationS: Double,
    )

    data class Calibration(
        val biasX: Double,
        val biasY: Double,
        val biasZ: Double,
        val noiseStd: Double,
    )

    fun toJson(t: TripInfo): String = buildString {
        append("{")
        append("\"trip_id\":\"").append(t.tripId).append("\",")
        append("\"device_id\":\"").append(t.deviceId).append("\",")
        append("\"app_version\":\"").append(t.appVersion).append("\",")
        append("\"model\":\"").append(t.model).append("\",")
        append("\"android\":").append(t.android).append(",")
        append("\"vehicle_type\":\"").append(t.vehicleType).append("\",")
        append("\"mount_type\":\"").append(t.mountType).append("\",")
        append("\"started_at\":\"").append(t.startedAtIso).append("\",")
        append("\"ended_at\":\"").append(t.endedAtIso).append("\",")
        append("\"calib\":")
        if (t.calib == null) {
            append("null")
        } else {
            append("{")
            append("\"bias_x\":").append(Bins.round(t.calib.biasX)).append(",")
            append("\"bias_y\":").append(Bins.round(t.calib.biasY)).append(",")
            append("\"bias_z\":").append(Bins.round(t.calib.biasZ)).append(",")
            append("\"noise_std\":").append(Bins.round(t.calib.noiseStd))
            append("}")
        }
        append(",\"effective_hz\":").append(Bins.round(t.effectiveHz))
        append(",\"rows\":").append(t.rows)
        append(",\"duration_s\":").append(Bins.round(t.durationS))
        append("}")
    }
}

/**
 * 30 s stationary calibration. Assumption: the phone rests flat on a surface
 * with +z up, so the gravity vector is ~(0, 0, +9.80665) in the sensor frame;
 * bias = mean(acc sample) - gravity. noiseStd = RMS of the de-biased residual.
 * The on-device pipeline is insensitive to the bias anyway (gravity is removed
 * by the 0.5 Hz low-pass), so this is a quality/health metric.
 */
object Calibrator {
    const val DURATION_S = 30.0
    private const val G = 9.80665

    class Result(val calib: TripMetadata.Calibration, val residualsStd: Double)

    fun compute(samples: List<Triple<Double, Double, Double>>): Result? {
        if ( samples.size < 100) return null
        var mx = 0.0; var my = 0.0; var mz = 0.0
        for ((x, y, z) in samples) { mx += x; my += y; mz += z }
        val n = samples.size
        mx /= n; my /= n; mz /= n
        var s = 0.0
        for ((x, y, z) in samples) s += (x - mx) * (x - mx) + (y - my) * (y - my) + (z - mz) * (z - mz)
        val noise = sqrt(s / n)
        return Result(
            TripMetadata.Calibration(
                biasX = mx,
                biasY = my,
                biasZ = mz - G,
                noiseStd = noise,
            ),
            residualsStd = noise,
        )
    }
}
