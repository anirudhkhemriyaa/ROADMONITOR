# Road Monitor 🛣️📱

A modular end-to-end platform for mobile road surface monitoring, sensor telemetry logging, and drive condition analysis.

---

## 📁 Monorepo Structure

`
Road-Monitor/
├── .github/
│   └── workflows/
│       └── build-android.yml    # CI: builds Android APK on push & PR
├── android/                     # Native Android Sensor Logger app (Kotlin)
│   ├── app/
│   │   ├── src/main/java/com/example/apptest22/
│   │   │   ├── MainActivity.kt  # UI controls & permission management
│   │   │   ├── LoggerService.kt # Foreground service logging IMU & GPS to CSV
│   │   │   └── Uploader.kt      # HTTP multipart uploader to backend
│   │   └── AndroidManifest.xml
│   ├── build.gradle
│   ├── settings.gradle
│   └── gradlew / gradlew.bat    # Gradle 8.7 wrapper
├── backend/                     # FastAPI ingestion & drive analysis server
│   ├── app.py                   # REST endpoints: /upload, /drives, /ping
│   └── requirements.txt
├── phone/                       # Headless data collection scripts
│   ├── logger.py                # Termux sensor logger via Termux API
│   ├── start.sh                 # Background execution script
│   └── stop.sh                  # Graceful shutdown script
├── web/                         # React 18 + Vite 5 frontend
│   ├── src/                     # Web application source
│   ├── vite.config.js           # Includes dev proxy to :8000 backend
│   └── package.json
├── data/                        # Drive data directory
│   └── raw/                     # Drive summary JSONs and marker annotations
└── docs/                        # Project documentation & assets
    ├── project-report/          # Academic and technical reports
    ├── diagrams/                # System architecture and flow charts
    ├── presentation/            # Slides and review materials
    ├── screenshots/             # Application screenshots & captures
    └── android-app.zip          # Standalone Android application archive
`

---

## 🚀 Implemented Features

### 1. Android Sensor Logger (`android/`)
- **100% Kotlin codebase** targeting Android 14 (API 34, minSdk 26).
- **Zero external dependencies**: Built purely on core Android platform APIs.
- **Continuous Foreground Service**: `LoggerService` records telemetry uninterrupted while screen is off.
- **Sensor Telemetry**:
  - Linear Acceleration & Gravity (`Sensor.TYPE_ACCELEROMETER`)
  - Angular Velocity (`Sensor.TYPE_GYROSCOPE`)
  - Magnetic Field (`Sensor.TYPE_MAGNETIC_FIELD`)
  - Geospatial Coordinates (`LocationManager.GPS_PROVIDER` & `NETWORK_PROVIDER`)
- **Local Storage**: Streams real-time readings into CSV files on device storage.
- **HTTP Multipart Upload**: `Uploader.kt` securely transfers completed drive recordings to the backend.

### 2. FastAPI Ingestion Backend (`backend/`)
- High-performance asynchronous REST API powered by FastAPI & Uvicorn.
- **`POST /upload`**: Receives multi-part sensor archives, extracts files into `data/raw/`, and generates telemetry summaries.
- **`GET /drives`**: Lists all recorded sessions with calculated metadata (sample count, total duration, geographic bounding box).
- **`GET /drives/{drive_id}` & `/csv`**: Retrieves detailed drive metrics or streams raw CSV telemetry.
- **`GET/POST /drives/{drive_id}/markers`**: Query or submit road anomaly tags (potholes, speed bumps, road defects).
- **`GET /ping`**: Server health-check probe.

### 3. Termux Phone Scripts (`phone/`)
- Headless Python logging scripts utilizing the Termux API for automated or background test fixtures without UI overhead.

### 4. Web Application (`web/`)
- Single-page application built on React 18 and Vite 5.
- Development proxy configured to seamlessly route `/api`, `/upload`, and `/ping` to the FastAPI backend on port 8000.

### 5. Automated CI/CD (`.github/workflows/`)
- GitHub Actions pipeline runs on every push and pull request to `main`.
- Automates Gradle 8.7 / JDK 17 setup, compiles the native Kotlin app, and publishes `app-debug.apk` directly to GitHub workflow artifacts.

---

## 🛠️ Getting Started

### Prerequisites
- **Android**: Android Studio Jellyfish or newer / JDK 17
- **Backend**: Python 3.10+
- **Web**: Node.js 18+ and npm

---

### Running the Android App

#### Option A: Local Build with Gradle Wrapper
`ash
cd android
./gradlew assembleDebug      # Linux / macOS
gradlew.bat assembleDebug   # Windows
`
The compiled APK will be generated at:
`android/app/build/outputs/apk/debug/app-debug.apk`

#### Option B: GitHub Actions (No local Android SDK needed)
Push changes to GitHub and download the pre-compiled `app-debug.apk` from the **Actions** tab.

---

### Running the Backend Server

`ash
# 1. Install dependencies
pip install -r backend/requirements.txt

# 2. Start FastAPI development server
python backend/app.py
`
The server will start listening at `http://localhost:8000`. Interactive OpenAPI documentation will be accessible at `http://localhost:8000/docs`.

---

### Running the Web Frontend

`ash
# 1. Navigate to web directory
cd web

# 2. Install dependencies
npm install

# 3. Start Vite dev server
npm run dev
`
Open `http://localhost:5173` in your browser. All API calls to `/api` or `/upload` will be proxied to `http://localhost:8000`.

---

## 📊 Data Format

Drive recordings are stored as CSV files with synchronized timestamps:
- `timestamp`: Unix millisecond timestamp
- `acc_x, acc_y, acc_z`: Linear acceleration in m/s²
- `gyro_x, gyro_y, gyro_z`: Angular rate in rad/s
- `mag_x, mag_y, mag_z`: Geomagnetic field in µT
- `latitude, longitude, altitude, speed`: GPS telemetry

Large raw CSV files are excluded from Git version control; sample drive summaries (`*.summary.json`) and anomaly markers (`*_markers.csv`) are tracked in `data/raw/`.

---

## 🗺️ Planned Roadmap

The following modules are planned for subsequent iterations:
- [ ] **Interactive GIS Map**: Integrate Mapbox / Leaflet in `web/` to display color-coded road quality heatmaps.
- [ ] **Automated Anomaly Detection**: Machine learning model (Random Forest / CNN) for automated detection of potholes and speed bumps from vertical acceleration spikes.
- [ ] **Persistent Database**: Transition backend storage from flat filesystem files to SQLite / PostgreSQL.
- [ ] **Real-time Live Streaming**: WebSocket telemetry streaming from mobile devices directly to web dashboard.
