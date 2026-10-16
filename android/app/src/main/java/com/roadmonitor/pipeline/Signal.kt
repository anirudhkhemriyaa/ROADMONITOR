package com.roadmonitor.pipeline

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.sqrt

/**
 * DSP building blocks. Pure Kotlin (no Android imports) so it is unit-testable
 * on the JVM. Mirrors the plan section 5.1 / 5.2 algorithms exactly.
 */
object Signal {

    // ---- resampling ---------------------------------------------------------

    /**
     * Resamples an irregularly-sampled signal to a uniform grid at [hz], by
     * linear interpolation. Gaps longer than [maxGapMs] start a new segment
     * instead of being bridged.
     * Returns the list of (unixMillis grid, resampled values) segments.
     */
    fun resampleUniform(
        tMs: DoubleArray,
        v: DoubleArray,
        hz: Double = RoadConfig.ACCEL_HZ,
        maxGapMs: Double = RoadConfig.RESAMPLE_MAX_GAP_MS,
    ): List<Pair<DoubleArray, DoubleArray>> {
        require(tMs.size == v.size) { "tMs/v size mismatch" }
        if (tMs.size < 2) return emptyList()
        val step = 1000.0 / hz
        val out = ArrayList<Pair<DoubleArray, DoubleArray>>()
        var segStart = 0
        var i = 1
        while (i < tMs.size) {
            if (tMs[i] - tMs[i - 1] > maxGapMs) {
                addSegment(out, tMs, v, segStart, i, step)
                segStart = i
            }
            i++
        }
        addSegment(out, tMs, v, segStart, tMs.size, step)
        return out
    }

    private fun addSegment(
        out: MutableList<Pair<DoubleArray, DoubleArray>>,
        tMs: DoubleArray,
        v: DoubleArray,
        s: Int,
        e: Int,
        step: Double,
    ) {
        if (e - s < 2) return
        val t0 = tMs[s]
        val t1 = tMs[e - 1]
        if (t1 <= t0) return
        val n = ((t1 - t0) / step).toInt() + 1
        if (n < 2) return
        val grid = DoubleArray(n)
        val vals = DoubleArray(n)
        var j = s
        for (k in 0 until n) {
            val g = t0 + k * step
            grid[k] = g
            while (j < e - 1 && tMs[j + 1] < g) j++
            val x0 = tMs[j]
            val x1 = tMs[minOf(j + 1, e - 1)]
            val y0 = v[j]
            val y1 = v[minOf(j + 1, e - 1)]
            vals[k] = if (x1 > x0) y0 + (y1 - y0) * (g - x0) / (x1 - x0) else y0
        }
        out.add(grid to vals)
    }

    /** Nearest-neighbour interpolation of [v] (sampled at [tMs]) onto [grid]. */
    fun nearestOn(tMs: DoubleArray, v: DoubleArray, grid: DoubleArray): DoubleArray {
        val out = DoubleArray(grid.size)
        if (tMs.isEmpty()) return out
        var j = 0
        for (k in grid.indices) {
            while (j < tMs.size - 1 && abs(tMs[j + 1] - grid[k]) <= abs(grid[k] - tMs[j])) j++
            out[k] = v[j]
        }
        return out
    }

    // ---- filters ------------------------------------------------------------

    /**
     * 2nd-order Butterworth band-pass (single biquad section), zero-phase
     * (forward + backward pass, i.e. filtfilt) with reflective edge padding.
     ** Equivalent to scipy.signal.sosfiltfilt with one SOS.
     */
    fun bandpassFiltFilt(
        y: DoubleArray,
        fs: Double = RoadConfig.ACCEL_HZ,
        low: Double = RoadConfig.Filter.BANDPASS_LOW,
        high: Double = RoadConfig.Filter.BANDPASS_HIGH,
    ): DoubleArray {
        val f0 = sqrt(low * high)
        val q = f0 / (high - low)
        return biquadFiltFilt(y, fs, f0, q)
    }

    /** RBJ audio-EQ biquad band-pass applied zero-phase with edge padding. */
    private fun biquadFiltFilt(
        y: DoubleArray,
        fs: Double,
        f0: Double,
        q: Double,
    ): DoubleArray {
        val pad = RoadConfig.Filter.PAD
        val n = y.size
        if (n < 2 * pad + 2) return DoubleArray(n)
        val w0 = 2.0 * PI * f0 / fs
        val alpha = kotlin.math.sin(w0) / (2.0 * q)
        val a0 = 1.0 + alpha
        val b0 = alpha / a0
        val b1 = 0.0
        val b2 = -alpha / a0
        val a1 = -2.0 * kotlin.math.cos(w0) / a0
        val a2 = (1.0 - alpha) / a0

        // reflective padding (mirror first/last [pad] samples)
        val padded = DoubleArray(n + 2 * pad)
        for (i in 0 until pad) padded[i] = y[pad - i]
        y.copyInto(padded, pad, 0, n)
        for (i in 0 until pad) padded[pad + n + i] = y[n - 2 - i]

        val fwd = biquad(padded, b0, b1, b2, a1, a2)
        val rev = DoubleArray(fwd.size) { i -> fwd[fwd.size - 1 - i] }
        val back = biquad(rev, b0, b1, b2, a1, a2)
        val out = DoubleArray(n)
        for (i in 0 until n) out[i] = back[back.size - 1 - (pad + n - 1 - i)]
        return out
    }

    /** Direct form II transposed biquad (a1, a2 only; a0 normalised to 1). */
    private fun biquad(
        x: DoubleArray,
        b0: Double, b1: Double, b2: Double,
        a1: Double, a2: Double,
    ): DoubleArray {
        val out = DoubleArray(x.size)
        var s1 = 0.0
        var s2 = 0.0
        for (i in x.indices) {
            val res = b0 * x[i] + s1
            s1 = b1 * x[i] - a1 * res + s2
            s2 = b2 * x[i] - a2 * res
            out[i] = res
        }
        return out
    }

    /**
     * Gravity low-pass: 1st-order IIR at [fc] Hz, applied forward then backward
     * (zero phase, matching a scipy filtfilt with a single pole).
     */
    fun gravityLowpass(
        x: DoubleArray,
        fs: Double = RoadConfig.ACCEL_HZ,
        fc: Double = RoadConfig.Filter.GRAVITY_LP_HZ,
    ): DoubleArray {
        val n = x.size
        if (n == 0) return x
        val alpha = exp(-2.0 * PI * fc / fs)
        val fwd = iir(x, alpha)
        val rev = DoubleArray(n) { i -> fwd[n - 1 - i] }
        val back = iir(rev, alpha)
        return DoubleArray(n) { i -> back[n - 1 - i] }
    }

    private fun iir(x: DoubleArray, alpha: Double): DoubleArray {
        val out = DoubleArray(x.size)
        var acc = x[0]
        out[0] = acc
        for (i in 1 until x.size) {
            acc = alpha * acc + (1.0 - alpha) * x[i]
            out[i] = acc
        }
        return out
    }

    // ---- vertical axis projection (plan 5.1) --------------------------------

    class VertAxis(
        val aVert: DoubleArray,
        /** Magnitude of the horizontal (gravity-orthogonal) acceleration. */
        val horizMag: DoubleArray,
        val gravityNorm: DoubleArray,
    )

    /**
     * g = lowpass(acc, 0.5 Hz); v = g / |g|;
     * a_vert = dot(acc, v) - |g|; horiz = acc - dot(acc, v) * v.
     */
    fun verticalAxis(
        ax: DoubleArray,
        ay: DoubleArray,
        az: DoubleArray,
        fs: Double = RoadConfig.ACCEL_HZ,
    ): VertAxis {
        val n = ax.size
        val gx = gravityLowpass(ax, fs)
        val gy = gravityLowpass(ay, fs)
        val gz = gravityLowpass(az, fs)
        val aVert = DoubleArray(n)
        val horiz = DoubleArray(n)
        val gn = DoubleArray(n)
        for (i in 0 until n) {
            val g = sqrt(gx[i] * gx[i] + gy[i] * gy[i] + gz[i] * gz[i])
            gn[i] = g
            val inv = if (g > 1e-9) 1.0 / g else 0.0
            val vx = gx[i] * inv
            val vy = gy[i] * inv
            val vz = gz[i] * inv
            val dot = ax[i] * vx + ay[i] * vy + az[i] * vz
            aVert[i] = dot - g
            val hx = ax[i] - dot * vx
            val hy = ay[i] - dot * vy
            val hz = az[i] - dot * vz
            horiz[i] = sqrt(hx * hx + hy * hy + hz * hz)
        }
        return VertAxis(aVert, horiz, gn)
    }

    // ---- other helpers ------------------------------------------------------

    /** Sliding standard deviation with window [win] (causal, min-count 1). */
    fun rollingStd(x: DoubleArray, win: Int): DoubleArray {
        val n = x.size
        val out = DoubleArray(n)
        var s = 0.0
        var s2 = 0.0
        var cnt = 0
        for (i in 0 until n) {
            s += x[i]; s2 += x[i] * x[i]; cnt++
            if (cnt > win) {
                val j = i - win
                s -= x[j]; s2 -= x[j] * x[j]; cnt--
            }
            val mean = s / cnt
            val varr = s2 / cnt - mean * mean
            out[i] = sqrt(max(varr, 0.0))
        }
        return out
    }

    /** Radix-2 iterative FFT. [re] holds the real part, [im] the imaginary part. */
    fun fft(re: DoubleArray, im: DoubleArray) {
        val n = re.size
        require(n and (n - 1) == 0) { "fft size must be power of two" }
        // bit reversal
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
            j = j or bit
            if (i < j) {
                val tr = re[i]; re[i] = re[j]; re[j] = tr
                val ti = im[i]; im[i] = im[j]; im[j] = ti
            }
        }
        var len = 2
        while (len <= n) {
            val twiddleStep = 2.0 * PI / len
            var i = 0
            while (i < n) {
                for (k in 0 until len / 2) {
                    val ang = twiddleStep * k
                    val wr = kotlin.math.cos(ang)
                    val wi = -kotlin.math.sin(ang)
                    val a = i + k
                    val b = i + k + len / 2
                    val tr = re[b] * wr - im[b] * wi
                    val ti = re[b] * wi + im[b] * wr
                    re[b] = re[a] - tr
                    im[b] = im[a] - ti
                    re[a] += tr
                    im[a] += ti
                }
                i += len
            }
            len = len shl 1
        }
    }

    /** Mean power in [lo, hi) Hz of a (real) segment, Hann-windowed, zero-padded to a power of two. */
    fun bandEnergy(seg: DoubleArray, fs: Double, lo: Double, hi: Double): Double {
        val n = seg.size
        if (n < 8) return 0.0
        var m = 1; while (m < n) m = m shl 1
        val windowed = DoubleArray(n) { i ->
            val w = 0.5 * (1.0 - kotlin.math.cos(2.0 * PI * i / (n - 1)))
            seg[i] * w
        }
        val re = DoubleArray(m); windowed.copyInto(re)
        val im = DoubleArray(m)
        fft(re, im)
        val half = m / 2
        var power = 0.0
        var count = 0
        for (k in 0 until half) {
            val f = k.toDouble() * fs / m
            if (f >= lo && f < hi) { power += re[k] * re[k] + im[k] * im[k]; count++ }
        }
        // one-sided PSD estimate, normalised per sample (comparable across bins)
        return if (count == 0) 0.0 else power * 4.0 / m / fs
    }

    /** P percentile of an already-sorted array (linear interpolation). */
    fun percentileSorted(sorted: DoubleArray, p: Double): Double {
        if (sorted.isEmpty()) return 0.0
        val idx = p / 100.0 * (sorted.size - 1)
        val lo = idx.toInt()
        val hi = minOf(lo + 1, sorted.size - 1)
        val frac = idx - lo
        return sorted[lo] * (1 - frac) + sorted[hi] * frac
    }

    /** Turns unknown speeds (NaN, negative) into the last known value (0 at start). */
    fun fillSpeed(speedMps: DoubleArray): DoubleArray {
        val out = DoubleArray(speedMps.size)
        var last = 0.0
        var any = false
        for (i in speedMps.indices) {
            val v = speedMps[i]
            if (!v.isNaN() && v >= 0) { last = v; any = true }
            out[i] = if (any) last else 0.0
        }
        return out
    }

    fun haversine(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6371000.0
        val dLat = (lat2 - lat1) * PI / 180.0
        val dLon = (lon2 - lon1) * PI / 180.0
        val s = 0.5 * (1 - cos(dLat)) + cos(lat1 * PI / 180.0) * cos(lat2 * PI / 180.0) *
            0.5 * (1 - cos(dLon))
        return 2.0 * r * atan2(sqrt(s), sqrt(1 - s))
    }
}
