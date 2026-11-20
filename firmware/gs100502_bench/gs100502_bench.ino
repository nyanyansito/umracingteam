// Standalone GS100502 wheel speed test for STM32F407VGT6 / STM32duino.
#if !defined(USBD_USE_CDC) || defined(DISABLE_GENERIC_SERIALUSB)
#error "Select USB support: CDC (generic Serial supersede U(S)ART)."
#endif

constexpr uint32_t SENSOR_PIN = PA0;
constexpr uint32_t REPORT_MS = 500;
// Count the falling edges during one full wheel turn. For a ring mounted on
// the wheel with one pulse per tooth, use the ring's tooth count.
constexpr uint32_t PULSES_PER_REVOLUTION = 0;
// Measure how far the vehicle rolls during one full turn of the tyre.
constexpr float TYRE_ROLLING_CIRCUMFERENCE_M = 0.0f;
volatile uint32_t pulseCount = 0;
uint32_t previousCount = 0;
uint32_t previousMs = 0;

void onPulse() {
  ++pulseCount;  // One falling edge per pulse; keep serial I/O out of ISR.
}

void setup() {
  Serial.begin(115200);
  pinMode(SENSOR_PIN, INPUT);  // External pull-up and divider required.
  attachInterrupt(digitalPinToInterrupt(SENSOR_PIN), onPulse, FALLING);
  previousMs = millis();
}

void loop() {
  const uint32_t now = millis();
  const uint32_t elapsed = now - previousMs;
  if (elapsed < REPORT_MS) return;

  noInterrupts();
  const uint32_t total = pulseCount;
  interrupts();
  const uint32_t pulses = total - previousCount;
  const float hz = pulses * (1000.0f / elapsed);

  Serial.print("GS100502,ms="); Serial.print(now);
  Serial.print(",level="); Serial.print(digitalRead(SENSOR_PIN));
  Serial.print(",pulses="); Serial.print(pulses);
  Serial.print(",total="); Serial.print(total);
  Serial.print(",hz="); Serial.print(hz, 2);
  Serial.print(",rpm=");
  if (PULSES_PER_REVOLUTION == 0) {
    Serial.println("UNCONFIGURED,speed_kmh=UNCONFIGURED");
  } else {
    const float rpm = hz * (60.0f / static_cast<float>(PULSES_PER_REVOLUTION));
    Serial.print(rpm, 2);
    Serial.print(",speed_kmh=");
    if (TYRE_ROLLING_CIRCUMFERENCE_M > 0.0f) {
      Serial.println(rpm * TYRE_ROLLING_CIRCUMFERENCE_M * 0.06f, 2);
    } else {
      Serial.println("UNCONFIGURED");
    }
  }

  previousCount = total;
  previousMs = now;
}
