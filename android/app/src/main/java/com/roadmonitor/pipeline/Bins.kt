package com.roadmonitor.pipeline

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

/**
 * 10 m bin summaries (plan section 5.4) and the JSONL serialisation of the
 * edge outputs (plan section 4.3). Pure Kotlin.
 */
object Bins {

    data class BinSummary(
        val tsStart: Long,   // unix ms
        val tsEnd: Long,
        val lat: Double?,    // position at the bin start (null without fix)
        val lon: Double?,
        val distM: Double,   // cumulative trip distance at the bin start (m)
        val rms: Double,
        val std: Double,
        val max: Double,
        val p95: Double,
        val kurtosis: Double,
        val band05: Double,   // 0.5 - 5 Hz
        val band515: Double,  // 5 - 15 Hz
        val band15p: Double,  // > 15 Hz
        val speedMean: Double, // m/s
        val n: Int,
    )

    /**
     * Bin the band-passed vertical acceleration by travelled distance.
     * @param grid unix ms grid
     * @param aVertF band-passed vertical acceleration on the grid
     * @param speedMps speed per sample (NaN = unknown; filled by nearest known)
     * @param latAnchor/lonAnchor (distM, lat, lon) GPS anchors - may be empty
     */
    fun summarize(
        grid: DoubleArray,
        aVertF: DoubleArray,
        speedMps: DoubleArray,
        anchors: List<DistanceAnchor>,
        binM: Double = RoadConfig.Roughness.BIN_M,
        minSamples: Int = RoadConfig.Roughness.MIN_SAMPLES_PER_BIN,
    ): List<BinSummary> {
        val n = aVertF.size
        if (n == 0) return emptyList()

        val speed = fillSpeed(speedMps)
        val dist = cumulativeDistance(grid, speed)

        // group sample indices by bin
        val groups = LinkedHashMap<Int, MutableList<Int>>()
        for (i in 0 until n) {
            val b = (dist[i] / binM).toInt()
            groups.getOrPut(b) { ArrayList() }.add(i)
        }

        val fs = RoadConfig.ACCEL_HZ
        val out = ArrayList<BinSummary>()
        for ((binIdx, indices) in groups) {
            if (indices.size < minSamples) continue
            val vals = DoubleArray(indices.size) { aVertF[indices[it]] }
            val absVals = vals.map { abs(it) }.sorted().toDoubleArray()
            var mean = 0.0
            for (v in vals) mean += v
            mean /= vals.size
            var m2 = 0.0; var m4 = 0.0
            var mx = 0.0
            for (v in vals) {
                val dv = v - mean
                m2 += dv * dv; m4 += dv * dv * dv * dv
                mx = max(mx, abs(v))
            }
            m2 /= vals.size
            m4 /= vals.size
            val std = sqrt(m2)
            val kurt = if (m2 > 0) m4 / (m2 * m2) - 3.0 else 0.0
            var sumSpeed = 0.0
            for (i in indices) sumSpeed += speed[i]
            val anchor = anchorAt(anchors, dist[indices.first()])
            out.add(
                BinSummary(
                    tsStart = grid[indices.first()].toLong(),
                    tsEnd = grid[indices.last()].toLong(),
                    lat = anchor?.lat,
                    lon = anchor?.lon,
                    distM = dist[indices.first()],
                    rms = sqrt(vals.map { it * it }.average()),
                    std = std,
                    max = mx,
                    p95 = Signal.percentileSorted(absVals, 95.0),
                    kurtosis = kurt,
                    band05 = Signal.bandEnergy(vals, fs, 0.5, 5.0),
                    band515 = Signal.bandEnergy(vals, fs, 5.0, 15.0),
                    band15p = Signal.bandEnergy(vals, fs, 15.0, Double.POSITIVE_INFINITY),
                    speedMean = if (indices.isEmpty()) 0.0 else sumSpeed / indices.size,
                    n = indices.size,
                ),
            )
        }
        return out
    }

    data class DistanceAnchor(val distM: Double, val lat: Double, val lon: Double)

    private fun anchorAt(anchors: List<DistanceAnchor>, distM: Double): DistanceAnchor? {
        if (anchors.isEmpty()) return null
        var best: DistanceAnchor? = null
        var bestD = Double.MAX_VALUE
        for (a in anchors) {
            val d = abs(a.distM - distM)
            if (d < bestD) { bestD = d; best = a }
        }
        return best
    }

    private fun fillSpeed(speedMps: DoubleArray): DoubleArray =
        Signal.fillSpeed(speedMps)

    /** Cumulative distance per sample from speed * dt (metres). */
    private fun cumulativeDistance(grid: DoubleArray, speed: DoubleArray): DoubleArray {
        val dist = DoubleArray(grid.size)
        for (i in 1 until grid.size) {
            val dt = (grid[i] - grid[i - 1]) / 1000.0
            dist[i] = dist[i - 1] + (speed[i - 1] + speed[i]) * 0.5 * dt
        }
        return dist
    }

    /**
     * Privacy trim (plan section 6 / Phase A item 6): keep only samples whose
     * cumulative distance is inside [trimM, total - trimM].
     */
    fun publicTrimMask(dist: DoubleArray, trimM: Double = RoadConfig.Gps.TRIM_START_END_M): BooleanArray {
        val total = dist.lastOrNull() ?: 0.0
        return BooleanArray(dist.size) { dist[it] >= trimM && dist[it] <= total - trimM }
    }

    // ---- JSONL --------------------------------------------------------------

    fun toJson(e: Events.EventFeatures): String = buildString {
        append("{\"ts\":").append(e.ts)
        append(",\"lat\":").append(e.lat ?: "null")
        append(",\"lon\":").append(e.lon ?: "null")
        append(",\"peak\":").append(fmt(e.peak))
        append(",\"p2p\":").append(fmt(e.p2p))
        append(",\"width_ms\":").append(fmt(e.widthMs))
        append(",\"jerk\":").append(fmt(e.jerk))
        append(",\"energy\":").append(fmt(e.energy))
        append(",\"speed\":").append(fmt(e.speed))
        append(",\"gyro_peak\":").append(fmt(e.gyroPeak))
        append(",\"speed_drop\":").append(fmt(e.speedDrop))
        append(",\"dist_m\":").append(fmt(e.distM))
        append(",\"type_hint\":\"").append(e.typeHint).append("\"}")
    }

    fun toJson(b: BinSummary): String = buildString {
        append("{\"ts_start\":").append(b.tsStart)
        append(",\"ts_end\":").append(b.tsEnd)
        append(",\"lat\":").append(b.lat ?: "null")
        append(",\"lon\":").append(b.lon ?: "null")
        append(",\"dist_m\":").append(fmt(b.distM))
        append(",\"rms\":").append(fmt(b.rms))
        append(",\"std\":").append(fmt(b.std))
        append(",\"max\":").append(fmt(b.max))
        append(",\"p95\":").append(fmt(b.p95))
        append(",\"kurtosis\":").append(fmt(b.kurtosis))
        append(",\"band_0_5\":").append(fmt(b.band05))
        append(",\"band_5_15\":").append(fmt(b.band515))
        append(",\"band_15p\":").append(fmt(b.band15p))
        append(",\"speed_mean\":").append(fmt(b.speedMean))
        append(",\"n\":").append(b.n)
        append("}")
    }

    private fun fmt(v: Double): String = when {
        v.isNaN() -> "null"
        else -> round(v)
    }

    internal fun round(v: Double): String {
        if (v.isNaN() || v.isInfinite()) return "null"
        val s = String.format(java.util.Locale.US, "%.6f", v)
        var r = s
        while (r.endsWith('0') && r.contains('.')) r = r.dropLast(1)
        if (r.endsWith('.')) r = r.dropLast(1)
        return r.ifEmpty { "0" }
    }
}
