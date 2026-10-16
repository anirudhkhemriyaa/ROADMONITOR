plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}
android {
    namespace = "com.roadmonitor"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.roadmonitor"
        minSdk = 26
        targetSdk = 34
        versionCode = 2
        versionName = "2.0.0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.work:work-runtime:2.9.1")
    testImplementation("junit:junit:4.13.2")
}
