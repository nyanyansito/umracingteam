#include <WiFi.h>

// Match firmware/telemetry_wifi_esp32 on the car.
constexpr char CAR_SSID[] = "UMR-GPS-Test";
constexpr char CAR_PASSWORD[] = "UMR-Bench-2026";
const IPAddress CAR_ADDRESS(192, 168, 4, 1);
constexpr uint16_t CAR_PORT = 9000;
constexpr uint32_t RETRY_MS = 2000;
constexpr int32_t CONNECT_TIMEOUT_MS = 250;
constexpr size_t READ_BUDGET = 512;

WiFiClient car;
uint8_t frame[160];
size_t frameLength = 0;
bool discardFrame = false;
uint32_t lastAttemptMs = 0;

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(CAR_SSID, CAR_PASSWORD);
  lastAttemptMs = millis() - RETRY_MS;
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    car.stop();
    frameLength = 0;
    discardFrame = false;
    delay(20);
    return;
  }

  if (!car.connected() && !car.available()) {
    frameLength = 0;
    discardFrame = false;
    const uint32_t now = millis();
    if (now - lastAttemptMs < RETRY_MS) {
      delay(5);
      return;
    }
    lastAttemptMs = now;
    car.stop();
    if (!car.connect(CAR_ADDRESS, CAR_PORT, CONNECT_TIMEOUT_MS)) {
      return;
    }
    car.setNoDelay(true);
  }

  // Keep line boundaries intact across disconnects. The car starts each new
  // TCP connection at a frame boundary. Forward timestamped bytes unchanged.
  for (size_t count = 0; count < READ_BUDGET && car.available(); ++count) {
    const int byte = car.read();
    if (byte < 0) break;

    if (!discardFrame && frameLength < sizeof(frame)) {
      frame[frameLength++] = static_cast<uint8_t>(byte);
    } else {
      discardFrame = true;
    }

    if (byte == '\n') {
      if (!discardFrame) Serial.write(frame, frameLength);
      frameLength = 0;
      discardFrame = false;
    }
  }
  delay(1);
}
