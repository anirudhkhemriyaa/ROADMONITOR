package com.roadmonitor.pipeline

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Event detection, plan section 5.3. Pure Kotlin.
 */
object Events {

    data class EventFeatures(
        val ts: Long,          // unix ms
        val lat: Double?,      // GPS at the trigger (null without fix)
        val lon: Double?,
        val peak: Double,      // |aVert| max, m/s^2 (band-passed)
        val p2p: Double,       // peak-to-peak inside the pulse
        val widthMs: Double,   // width at half max
        val jerk: Double,      // max |d(a)/dt|, m/s^3
        val energy: Double,    // sum a^2 dt
        val speed: Double,     // speed at the trigger, m/s
        val gyroPeak: Double,  // max |gyro z| inside the pulse, rad/s
        val speedDrop: Double, // (mean speed 1 s before) - (mean speed 1 s after), m/s
        val typeHint: String,  // pothole_candidate | bump_candidate | rough
        val distM: Double = Double.NaN, // cumulative trip distance at the trigger (m)
    )

    private data class Cand(
        val gridIdx: Int,
        val windowStart: Int,
        val windowEnd: Int,
        val peak: Double,
        val min: Double,
        val max: Double,
        val minIdx: Int,
        val maxIdx: Int,
        val widthMs: Double,
        val jerk: Double,
        val energy: Double,
        val gyroPeak: Double,
    )

    /**
     * @param grid unix ms for every resampled sample (position i = grid[i])
     * @param aVertF band-passed vertical acceleration
     * @param speedMps speed for every sample; negative or NaN = unknown
     * @param gyroZ gyro z at every sample (aligned to the same grid)
     * @param lat/lon per sample; null entries allowed (no fix)
     */
    fun detect(
        grid: DoubleArray,
        aVertF: DoubleArray,
        speedMps: DoubleArray,
        gyroZ: DoubleArray,
        lat: Array<Double?>,
        lon: Array<Double?>,
        dist: DoubleArray = DoubleArray(grid.size),
    ): List<EventFeatures> {
        val n = aVertF.size
        if (n < 10) return emptyList()
        val sigma = Signal.rollingStd(aVertF, (RoadConfig.Event.SIGMA_WINDOW_S * RoadConfig.ACCEL_HZ).toInt())
        val fs = RoadConfig.ACCEL_HZ

        val cands = ArrayList<Cand>()
        var refractoryUntilMs = -1e9
        var i = 0
        while (i < n) {
            val thr = max(
                RoadConfig.Event.ABS_THRESHOLD_MS2,
                RoadConfig.Event.SIGMA_K * sigma[i],
            )
            if (abs(aVertF[i]) > thr && speedOk(speedMps[i])) {
                val c = expand(grid, i, aVertF, gyroZ, fs)
                if (c != null) {
                    if (grid[i] >= refractoryUntilMs) {
                        cands.add(c)
                        refractoryUntilMs = grid[(c.windowEnd - 1).coerceAtLeast(0)] +
                            RoadConfig.Event.REFRACTORY_MS
                    }
                }
            }
            i++
        }
        return cands.filter { it.gyroPeak <= RoadConfig.Event.GYRO_REJECT_RS }
            .map { toFeatures(grid, it, aVertF, speedMps, gyroZ, lat, lon, fs, dist) }
    }

    private fun speedOk(v: Double): Boolean =
        !v.isNaN() && v >= 0 && v * 3.6 > RoadConfig.Speed.MIN_KMH && v * 3.6 <= RoadConfig.Speed.MAX_KMH

    /** Half-max pulse expansion around the first trigger index. */
    private fun expand(
        grid: DoubleArray,
        trigger: Int,
        aVertF: DoubleArray,
        gyroZ: DoubleArray,
        fs: Double,
    ): Cand? {
        val n = aVertF.size
        val span = (RoadConfig.Event.WINDOW_SPAN_S * fs).toInt()
        val lo = max(0, trigger - span)
        val hi = min(n, trigger + span + 1)
        // find the absolute peak inside the window
        var peakIdx = trigger
        for (k in lo until hi) if (abs(aVertF[k]) > abs(aVertF[peakIdx])) peakIdx = k
        val peak = abs(aVertF[peakIdx])
        if (peak < RoadConfig.Event.ABS_THRESHOLD_MS2) return null

        val half = 0.5 * peak
        var lo2 = peakIdx
        var hi2 = peakIdx + 1
        while (lo2 > lo && abs(aVertF[lo2 - 1]) >= half) lo2--
        while (hi2 < hi && abs(aVertF[hi2]) >= half) hi2++

        val seg = aVertF.sliceArray(lo2..<hi2)
        if (seg.isEmpty()) return null
        var mn = seg[0]; var mx = seg[0]; var mnI = lo2; var mxI = lo2
        var j = 0
        for (v in seg) {
            if (v < mn) { mn = v; mnI = lo2 + j }
            if (v > mx) { mx = v; mxI = lo2 + j }
            j++
        }
        var gyro = 0.0
        for (k in lo2 until hi2) gyro = max(gyro, abs(gyroZ[k]))
        // jerk on a slightly wider window so the pulse slopes are included
        // (the half-max window can sit on the smooth apex of a slow pulse)
        val jlo = max(0, lo2 - 5)
        val jhi = min(n, hi2 + 5)
        var jerk = 0.0
        var energy = 0.0
        for (k in (jlo + 1) until jhi) {
            jerk = max(jerk, abs(aVertF[k] - aVertF[k - 1]) * fs)
        }
        for (k in lo2 until hi2) energy += aVertF[k] * aVertF[k]
        energy /= fs
        val widthMs = (hi2 - lo2) / fs * 1000.0
        return Cand(
            gridIdx = peakIdx, windowStart = lo2, windowEnd = hi2,
            peak = peak, min = mn, max = mx, minIdx = mnI, maxIdx = mxI,
            widthMs = widthMs, jerk = jerk, energy = energy, gyroPeak = gyro,
        )
    }

    private fun toFeatures(
        grid: DoubleArray,
        c: Cand,
        aVertF: DoubleArray,
        speedMps: DoubleArray,
        gyroZ: DoubleArray,
        lat: Array<Double?>,
        lon: Array<Double?>,
        fs: Double,
        dist: DoubleArray,
    ): EventFeatures {
        val idx = c.gridIdx
        val w = RoadConfig.Event.SPEED_DROP_WINDOW_S
        val before = meanSpeed(speedMps, (idx - w * fs).toInt().coerceAtLeast(0), idx)
        val after = meanSpeed(
            speedMps, idx,
            (idx + w * fs).toInt().coerceIn(0, speedMps.size - 1).coerceAtLeast(idx),
        )
        val type = typeHint(c, gyroZ)
        return EventFeatures(
            ts = grid[idx].toLong(),
            lat = lat[idx],
            lon = lon[idx],
            peak = c.peak,
            p2p = c.max - c.min,
            widthMs = c.widthMs,
            jerk = c.jerk,
            energy = c.energy,
            speed = speedMps[idx].coerceAtLeast(0.0),
            gyroPeak = c.gyroPeak,
            speedDrop = before - after,
            typeHint = type,
            distM = dist.getOrNull(idx) ?: Double.NaN,
        )
    }

    /** Mean of known speeds in [from, to); returns NaN if none known. */
    private fun meanSpeed(speedMps: DoubleArray, from: Int, to: Int): Double {
        var s = 0.0
        var cnt = 0
        for (k in from..<to.coerceAtMost(speedMps.size)) {
            val v = speedMps[k]
            if (!v.isNaN() && v >= 0) { s += v; cnt++ }
        }
        return if (cnt == 0) Double.NaN else s / cnt
    }

    private fun typeHint(c: Cand, gyroZ: DoubleArray): String {
        val dipThenImpact = c.minIdx < c.maxIdx
        val inPotholeWidth = c.widthMs >= RoadConfig.Event.POTHOLE_WIDTH_MIN_MS &&
            c.widthMs <= RoadConfig.Event.POTHOLE_WIDTH_MAX_MS
        val inBumpWidth = c.widthMs >= RoadConfig.Event.BUMP_WIDTH_MIN_MS &&
            c.widthMs <= RoadConfig.Event.BUMP_WIDTH_MAX_MS
        var pitch = 0.0
        for (k in c.windowStart until c.windowEnd) pitch = max(pitch, abs(gyroZ[k]))
        return when {
            inPotholeWidth && dipThenImpact -> "pothole_candidate"
            inBumpWidth && (pitch > RoadConfig.Event.BUMP_GYRO_PITCH || !dipThenImpact) ->
                "bump_candidate"
            else -> "rough"
        }
    }
}
