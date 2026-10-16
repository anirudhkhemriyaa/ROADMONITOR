package com.roadmonitor.pipeline

/**
 * All thresholds. Mirrors backend/config.yaml (plan section 3). No other magic
 * numbers are allowed in the pipeline or service code.
 */
object RoadConfig {
    const val ACCEL_HZ = 50.0
    const val GPS_HZ = 1.0

    object Filter {
        const val BANDPASS_LOW = 0.5
        const val BANDPASS_HIGH = 20.0
        const val ORDER = 2
        const val GRAVITY_LP_HZ = 0.5
        /** Padding length (samples) for the reflective edge padding used by filtfilt. */
        const val PAD = 30
    }

    object Speed {
        const val MIN_KMH = 5.0
        const val MAX_KMH = 120.0
    }

    object Event {
        const val ABS_THRESHOLD_MS2 = 3.0
        const val SIGMA_K = 3.5
        const val SIGMA_WINDOW_S = 5.0
        const val REFRACTORY_MS = 300.0
        const val POTHOLE_WIDTH_MIN_MS = 40.0
        const val POTHOLE_WIDTH_MAX_MS = 200.0
        const val BUMP_WIDTH_MIN_MS = 200.0
        const val BUMP_WIDTH_MAX_MS = 600.0
        /** Pitch-rate signature needed for the bump hint. */
        const val BUMP_GYRO_PITCH = 0.15
        /** Turn / brake rejection. */
        const val GYRO_REJECT_RS = 0.5
        /** Half-max pulse windows are searched at most this far around a trigger. */
        const val WINDOW_SPAN_S = 0.3
        /** Speed-drop window (1 s before vs after the pulse). */
        const val SPEED_DROP_WINDOW_S = 1.0
    }

    object Roughness {
        const val BIN_M = 10.0
        const val SPEED_ALPHA = 0.7
        const val MIN_SAMPLES_PER_BIN = 20
        /** Lowest speed (m/s) allowed in the speed normalisation denominator. */
        const val SPEED_FLOOR_MPS = 1.5
    }

    object Gps {
        const val MAX_ACCURACY_M = 25.0
        const val TRIM_START_END_M = 150.0
    }

    /** Resampling: gaps above this are not bridged (they start a new segment). */
    const val RESAMPLE_MAX_GAP_MS = 1500.0
}
