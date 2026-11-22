Uart gpsInput(PA_3, PA_2);       // RX, TX
Uart telemetryOut(PB_11, PB_10); // RX, TX

constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
char gpsLine[128];
size_t gpsLineLength = 0;
uint32_t lastHeartbeatMs = 0;

void setup() {
  gpsInput.begin(9600);
  telemetryOut.begin(115200);
  telemetryOut.println("STM32_ALIVE");
  lastHeartbeatMs = millis();
}

void loop() {
  while (gpsInput.available() > 0) {
    const char byte = static_cast<char>(gpsInput.read());
    if (byte == '$') {
      gpsLine[0] = byte;
      gpsLineLength = 1;
    } else if (gpsLineLength > 0) {
      if (byte == '\n') {
        // Send complete NMEA lines so the heartbeat cannot split a sentence.
        telemetryOut.write(reinterpret_cast<const uint8_t *>(gpsLine), gpsLineLength);
        telemetryOut.write('\n');
        gpsLineLength = 0;
      } else if (gpsLineLength < sizeof(gpsLine)) {
        gpsLine[gpsLineLength++] = byte;
      } else {
        gpsLineLength = 0;
      }
    }
  }

  const uint32_t now = millis();
  if (now - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    telemetryOut.println("STM32_ALIVE");
  }
}
