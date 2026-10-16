package com.roadmonitor.pipeline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin
import kotlin.math.absoluteValue
import kotlin.math.sqrt
import kotlin.random.Random

/**
 * Synthetic-signal tests for the Phase A pipeline (plan section 6 / 7.2 item 9).
 *
 * Conventions used here (matching LoggerService row semantics):
 * gravity at rest points along -z with magnitude ~9.81 (a_horiz-free tilt
 * scenario would need a rotated axis, so these tests keep the phone flat).
 */
class ProcessingTest {

    private val fs = RoadConfig.ACCEL_HZ

    /** Resting-state accel rows: support force along +z. */
    private fun flatSamples(
        seconds: Double,
        noiseStd: Double = 0.05,
        speedKmh: Double = 30.0,
        rng: Random = Random(42),
    ): List<RawSample> {
        val n = (seconds * fs).toInt()
        val g = 9.81
        val speed = speedKmh / 3.6
        val t0 = 1_700_000_000_000L
        return List(n) { i ->
            RawSample(
                tMs = t0 + (i * 1000.0 / fs).toLong(),
                tNs = i * 20_000_000L,
                ax = rng.nextDouble() * noiseStd,
                ay = rng.nextDouble() * noiseStd,
                az = g + rng.nextDouble() * noiseStd,
                gx = 0.0, gy = 0.0, gz = 0.0,
                gravX = 0.0, gravY = 0.0, gravZ = -g,
                speedMps = speed,
                lat = 23.0 + i * 1e-6, lon = 78.0, gpsAcc = 5.0,
                altitude = null, speedAcc = 1.0,
            )
        }
    }

    private fun addPothole(samples: MutableList<RawSample>, atS: Double, amp: Double = 8.0, widthMs: Double = 60.0) {
        val i = (atS * fs).toInt()
        val w = (widthMs / 1000.0 * fs).toInt()
        // support force along +z: the "dip" is a drop of az, the impact an overshoot
        for (k in 0 until w) {
            val v = amp * sin(PI * k / w)
            val c = samples[i + k]
            samples[i + k] = c.copy(az = c.az - v)          // dip first (support lost)
        }
        for (k in 0 until w / 2) {
            val v = amp * 0.9 * sin(PI * k / (w / 2))
            val c = samples[i + w + k]
            samples[i + w + k] = c.copy(az = c.az + v)      // impact after the dip
        }
    }

    @Test
    fun `flat road produces zero events`() {
        val samples = flatSamples(60.0)
        val events = ArrayList<Events.EventFeatures>()
        TripPipeline.process(samples) { events.add(it) }
        assertEquals(0.0, events.size.toDouble(), 0.0)
    }

    @Test
    fun `injected pothole pulse produces exactly one event with sane features`() {
        val samples = flatSamples(120.0).toMutableList()
        addPothole(samples, atS = 60.0)
        val events = ArrayList<Events.EventFeatures>()
        TripPipeline.process(samples) { events.add(it) }
        assertTrue("expected 1 event, got ${events.size}", events.size == 1)
        val e = events[0]
        assertTrue("peak ${e.peak} too small", e.peak > 4.0)
        assertTrue("width ${e.widthMs} off", e.widthMs in 20.0..400.0)
        assertTrue("jerk ${e.jerk} too small", e.jerk > 50.0)
        assertTrue(e.typeHint == "pothole_candidate" || e.typeHint == "rough")
    }

    @Test
    fun `turn only signal is rejected by gyro rule`() {
        val samples = flatSamples(60.0, noiseStd = 0.02).toMutableList()
        // constant horizontal acceleration + fast yaw -> must not create events
        for (k in samples.indices) {
            val c = samples[k]
            samples[k] = c.copy(ax = c.ax + 3.0, gz = 0.9)
        }
        val events = ArrayList<Events.EventFeatures>()
        TripPipeline.process(samples) { events.add(it) }
        assertEquals(0.0, events.size.toDouble(), 0.0)
    }

    @Test
    fun `genuine pulse with high gyro is rejected`() {
        val samples = flatSamples(60.0).toMutableList()
        addPothole(samples, atS = 30.0)
        for (k in samples.indices) {
            val c = samples[k]
            samples[k] = c.copy(gz = 0.9)   // fast turn everywhere
        }
        val events = ArrayList<Events.EventFeatures>()
        TripPipeline.process(samples) { events.add(it) }
        assertEquals(0.0, events.size.toDouble(), 0.0)
    }

    @Test
    fun `jittery timestamps resample without nan and lose no events`() {
        val base = flatSamples(60.0)
        val rng = Random(7)
        val jittered = base.mapIndexed { i, s ->
            val jitter = (rng.nextDouble() * 6.0 - 3.0).toLong()
            s.copy(tNs = s.tNs + jitter * 1_000_000, tMs = s.tMs + jitter)
        }
        val cleanEvents = ArrayList<Events.EventFeatures>()
        TripPipeline.process(jittered) { cleanEvents.add(it) }
        assertEquals(0.0, cleanEvents.size.toDouble(), 0.0)  // both flat -> still no events

        // with a pulse, detection must still work (not blocked by resample gap logic)
        val withPulse = jittered.toMutableList()
        addPothole(withPulse, atS = 30.0)
        val events2 = ArrayList<Events.EventFeatures>()
        TripPipeline.process(withPulse) { events2.add(it) }
        assertTrue("pulse lost under jitter", events2.size >= 1)
    }

    @Test
    fun `resampler splits multi-second gaps into separate segments`() {
        // 1 s of signal, 4 s gap, 1 s of signal
        val n = 50
        val t = DoubleArray(2 * n + 1) { i -> i * 20.0 }
        val v = DoubleArray(2 * n + 1) { i -> if (i < n) 1.0 else if (i > n) -1.0 else 0.0 }
        // insert the gap: shift the second half by 4000 ms
        val tShifted = DoubleArray(2 * n + 1) { i -> if (i <= n) t[i] else t[i] + 4000.0 }
        val segs = Signal.resampleUniform(tShifted, v)
        assertEquals(2, segs.size)
        for ((grid, vv) in segs) {
            for (x in vv) assertTrue(!x.isNaN())
        }
        // segment 1 spans 0..1000 ms -> 51 samples; segment 2 spans 5020..6000 -> 50
        assertEquals(51.0, segs[0].first.size.toDouble(), 0.001)
        assertEquals(50.0, segs[1].first.size.toDouble(), 0.001)
    }

    @Test
    fun `bandpass kills 22 hz tone and keeps 3 hz tone`() {
        val n = 20 * fs.toInt()
        val t = DoubleArray(n) { it / fs }
        val lo = DoubleArray(n) { 5.0 * sin(2 * PI * 3.0 * t[it]) }
        val hi = DoubleArray(n) { 5.0 * sin(2 * PI * 22.0 * t[it]) }
        val outLo = Signal.bandpassFiltFilt(lo)
        val outHi = Signal.bandpassFiltFilt(hi)
        val peakLo = (200 until n - 200).maxOf { outLo[it].absoluteValue }  // skip filter edges
        val peakHi = (200 until n - 200).maxOf { outHi[it].absoluteValue }
        assertTrue("3 Hz should pass, got $peakLo", peakLo > 4.0)
        assertTrue("22 Hz should be attenuated, got $peakHi", peakHi < 0.8)
    }

    @Test
    fun `vertical axis is zero at rest`() {
        val n = fs.toInt()
        val ax = DoubleArray(n) { 0.0 }
        val ay = DoubleArray(n) { 0.0 }
        val az = DoubleArray(n) { 9.81 + sin(0.0) }
        val v = Signal.verticalAxis(ax, ay, az)
        val center = (n / 4 until 3 * n / 4).maxOf { v.aVert[it].absoluteValue }
        assertTrue("aVert at rest should be ~0, got $center", center < 0.35)
        assertTrue("horiz at rest should be ~0, got ${v.horizMag.last()}", v.horizMag.last() < 0.35)
    }

    @Test
    fun `bins cover distance with minimum sample rule`() {
        // 60 s at 30 km/h = 500 m -> about 50 bins of 10 m, each >= 20 samples
        val samples = flatSamples(60.0)
        val bins = ArrayList<Bins.BinSummary>()
        TripPipeline.writeSegments(samples) { bins.add(it) }
        assertTrue("expected ~50 bins, got ${bins.size}", bins.size in 40..60)
        for (b in bins) {
            assertTrue("bin too small (${b.n})", b.n >= RoadConfig.Roughness.MIN_SAMPLES_PER_BIN)
            assertTrue("bin rms ${b.rms} too big", b.rms < 0.5)
            assertTrue(!b.speedMean.isNaN())
        }
        // ordering + distance monotonic
        for (i in 1 until bins.size) {
            assertTrue(bins[i].distM > bins[i - 1].distM)
            assertTrue(bins[i].tsStart >= bins[i - 1].tsStart)
        }
    }

    @Test
    fun `jsonl lines are valid json with required keys`() {
        val samples = flatSamples(60.0).toMutableList()
        addPothole(samples, atS = 30.0)
        val events = ArrayList<Events.EventFeatures>()
        val bins = ArrayList<Bins.BinSummary>()
        TripPipeline.process(samples) { events.add(it) }
        TripPipeline.writeSegments(samples) { bins.add(it) }
        assertTrue(events.isNotEmpty())
        val line = Bins.toJson(events[0])
        for (key in listOf("ts", "lat", "lon", "peak", "p2p", "width_ms", "jerk",
                           "energy", "speed", "gyro_peak", "speed_drop", "type_hint", "dist_m")) {
            assertTrue("missing key $key in $line", line.contains("\"$key\""))
        }
        assertTrue(line.startsWith("{") && line.endsWith("}"))
    }

    @Test
    fun `privacy trim cuts head and tail distances`() {
        val total = 500.0
        val dist = DoubleArray(1000) { it / 999.0 * total }
        val mask = Bins.publicTrimMask(dist)
        val span = dist[mask.withIndex().filter { it.value }.map { it.index }.last()] -
                dist[mask.indexOfFirst { it }]
        assertTrue("span ${span} m should be ~200 m", span < total - 2 * (RoadConfig.Gps.TRIM_START_END_M - 1.0))
        assertEquals(150.0, dist[mask.indexOfFirst { it }], 2.0)
        assertEquals(total - 150.0, dist[mask.withIndex().filter { it.value }.map { it.index }.last()], 2.0)
    }
}
