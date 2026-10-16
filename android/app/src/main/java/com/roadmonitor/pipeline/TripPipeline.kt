package com.roadmonitor.pipeline

import java.io.File
import kotlin.math.abs

/**
 * One raw sample from the logger. Gyro / gravity columns may come from a
 * slightly different timestamp: the service carries the nearest recent reading
 * onto the accel row and leaves the fields empty otherwise.
 */
data class RawSample(
    val tMs: Long,        // unix ms (derived from SensorEvent.timestamp)
    val tNs: Long,        // SensorEvent.timestamp (nanos since boot, raw)
    val ax: Double, val ay: Double, val az: Double,
    val gx: Double, val gy: Double, val gz: Double,
    val gravX: Double, val gravY: Double, val gravZ: Double,
    val speedMps: Double, // NaN = no fix
    val lat: Double?, val lon: Double?, val gpsAcc: Double?,
    val altitude: Double?, val speedAcc: Double?,
)

/**
 * Runs the on-device pipeline over the trip buffer and writes
 * {tripId}_events.jsonl / {tripId}_segments.jsonl next to the raw CSV
 * (plan section 4.3). Files stay untrimmed; upload-time privacy trimming is
 * done by Uploader using the `dist_m` fields.
 */
object TripPipeline {

    class Output(
        val eventsPath: String?,
        val segmentsPath: String?,
        val nEvents: Int,
        val nBins: Int,
        val error: String?,
    )

    private const val MIN_SAMPLES = 100

    /** Detects events from raw samples (used by unit tests and tooling). */
    fun process(samples: List<RawSample>): List<Events.EventFeatures> {
        val events = ArrayList<Events.EventFeatures>()
        // skim: reuse processAndWrite's segment walk without file I/O
        forEachGridSegment(samples) { grid, aVertF, speedMps, gyroZ, latA, lonA, dist ->
            events += Events.detect(grid, aVertF, speedMps, gyroZ, latA, lonA, dist)
        }
        return events
    }

    /** Produces 10 m bin summaries from raw samples (unit tests). */
    fun writeSegments(samples: List<RawSample>): List<Bins.BinSummary> {
        val bins = ArrayList<Bins.BinSummary>()
        forEachGridSegment(samples) { grid, aVertF, speedMps, _, latA, lonA, dist ->
            bins += Bins.summarize(grid, aVertF, speedMps, anchorsFor(grid, dist, latA, lonA))
        }
        return bins
    }

    /** Sink variant (streams each bin out instead of collecting them). */
    fun writeSegments(samples: List<RawSample>, sink: (Bins.BinSummary) -> Unit): Int =
        writeSegments(samples).also { list -> list.forEach(sink) }.size

    /** Convenience sink variant used by the service when files already stream out. */
    fun process(samples: List<RawSample>, sink: (Events.EventFeatures) -> Unit): Int =
        process(samples).also { list -> list.forEach(sink) }.size

    fun processAndWrite(dir: File, samples: List<RawSample>, tripId: String): Output {
        return try {
            val events = ArrayList<Events.EventFeatures>()
            val bins = ArrayList<Bins.BinSummary>()
            forEachGridSegment(samples) { grid, aVertF, speedMps, gyroZ, latA, lonA, dist ->
                events += Events.detect(grid, aVertF, speedMps, gyroZ, latA, lonA, dist)
                bins += Bins.summarize(grid, aVertF, speedMps, anchorsFor(grid, dist, latA, lonA))
            }
            val ef = if (events.isNotEmpty()) File(dir, "${tripId}_events.jsonl") else null
            val sf = if (bins.isNotEmpty()) File(dir, "${tripId}_segments.jsonl") else null
            ef?.writeText(events.joinToString("\n") { Bins.toJson(it) })
            sf?.writeText(bins.joinToString("\n") { Bins.toJson(it) })
            Output(ef?.absolutePath, sf?.absolutePath, events.size, bins.size, null)
        } catch (e: Exception) {
            Output(null, null, 0, 0, e.message ?: e.javaClass.simpleName)
        }
    }

    /**
     * Resamples all channels onto uniform 50 Hz segments (gaps start new
     * segments), computes the vertical/band-passed axis, the travelled
     * distance and the GPS anchors, and hands each segment to [block].
     */
    private fun forEachGridSegment(
        samples: List<RawSample>,
        block: (
            grid: DoubleArray,
            aVertF: DoubleArray,
            speedMps: DoubleArray,
            gyroZ: DoubleArray,
            lat: Array<Double?>,
            lon: Array<Double?>,
            dist: DoubleArray,
        ) -> Unit,
    ) {
        if (samples.size < MIN_SAMPLES) return

        // Defensive monotonic filter (clock jumps happen on some devices).
        val perm = ArrayList<Int>(samples.size)
        var last = -1L
        for (i in samples.indices) {
            if (samples[i].tMs >= last) { perm.add(i); last = samples[i].tMs }
        }
        if (perm.size < MIN_SAMPLES) return
        val idx = perm.toIntArray()
        val tMs = DoubleArray(idx.size) { samples[idx[it]].tMs.toDouble() }

        // ax..az live on the exact accel timestamps, which are also the master
        // clock, so a single uniform grid serves every channel (GPS speed is
        // carried onto each accel row by the service).
        val channels = Array(6) { ch -> channel(samples, idx, ch) }
        val speedCh = DoubleArray(idx.size) { samples[idx[it]].speedMps }

        for ((grid, _) in Signal.resampleUniform(tMs, speedCh)) {
            val n = grid.size
            if (n < MIN_SAMPLES) continue
            val axS = interpTo(tMs, channels[0], grid)
            val ayS = interpTo(tMs, channels[1], grid)
            val azS = interpTo(tMs, channels[2], grid)
            val gyroZ = nearestTo(tMs, channels[5], grid)   // raw gz
            val latA = arrayOfNulls<Double>(n)
            val lonA = arrayOfNulls<Double>(n)
            fillFixes(samples, idx, grid, latA, lonA)

            val vert = Signal.verticalAxis(axS, ayS, azS)
            val aVertF = Signal.bandpassFiltFilt(vert.aVert)
            val speedG = Signal.fillSpeed(interpTo(tMs, speedCh, grid))
            val dist = gridDistance(grid, speedG)
            block(grid, aVertF, speedG, gyroZ, latA, lonA, dist)
        }
    }

    /** Extracts one channel from the raw samples (0=ax..5=gz). */
    private fun channel(samples: List<RawSample>, idx: IntArray, ch: Int): DoubleArray =
        when (ch) {
            0 -> DoubleArray(idx.size) { samples[idx[it]].ax }
            1 -> DoubleArray(idx.size) { samples[idx[it]].ay }
            2 -> DoubleArray(idx.size) { samples[idx[it]].az }
            3 -> DoubleArray(idx.size) { samples[idx[it]].gx }
            4 -> DoubleArray(idx.size) { samples[idx[it]].gy }
            else -> DoubleArray(idx.size) { samples[idx[it]].gz }
        }

    private fun interpTo(tMs: DoubleArray, v: DoubleArray, grid: DoubleArray): DoubleArray {
        val out = DoubleArray(grid.size)
        var j = 0
        for (k in grid.indices) {
            while (j < tMs.size - 1 && tMs[j + 1] < grid[k]) j++
            val x0 = tMs[j]
            val x1 = tMs[minOf(j + 1, tMs.size - 1)]
            val y0 = v[j]
            val y1 = v[minOf(j + 1, tMs.size - 1)]
            out[k] = if (x1 > x0) y0 + (y1 - y0) * (grid[k] - x0) / (x1 - x0) else y0
        }
        return out
    }

    private fun nearestTo(tMs: DoubleArray, v: DoubleArray, grid: DoubleArray): DoubleArray {
        val out = DoubleArray(grid.size)
        var j = 0
        for (k in grid.indices) {
            while (j < tMs.size - 1 && abs(tMs[j + 1] - grid[k]) <= abs(grid[k] - tMs[j])) j++
            out[k] = v[j]
        }
        return out
    }

    /** Cumulative travelled distance per grid sample: trapezoidal speed
     * integration. Bins.summarize uses the same formula over the same filled
     * speed array, so bin distances and event distances agree. */
    private fun gridDistance(grid: DoubleArray, speed: DoubleArray): DoubleArray {
        val dist = DoubleArray(grid.size)
        for (i in 1 until grid.size) {
            val dt = (grid[i] - grid[i - 1]) / 1000.0
            dist[i] = dist[i - 1] + (speed[i - 1] + speed[i]) * 0.5 * dt
        }
        return dist
    }

    private fun fillFixes(
        samples: List<RawSample>,
        perm: IntArray,
        grid: DoubleArray,
        latA: Array<Double?>,
        lonA: Array<Double?>,
    ) {
        var j = 0
        for (k in grid.indices) {
            while (j < perm.size - 1 && samples[perm[j + 1]].tMs <= grid[k]) j++
            val s = samples[perm[j]]
            if (s.lat != null && s.lon != null) { latA[k] = s.lat; lonA[k] = s.lon }
        }
    }

    private fun anchorsFor(
        grid: DoubleArray,
        dist: DoubleArray,
        latA: Array<Double?>,
        lonA: Array<Double?>,
    ): List<Bins.DistanceAnchor> {
        val anchors = ArrayList<Bins.DistanceAnchor>()
        for (k in grid.indices) {
            val la = latA[k]
            val lo = lonA[k]
            if (la != null && lo != null) anchors.add(Bins.DistanceAnchor(dist[k], la, lo))
        }
        return anchors
    }
}
