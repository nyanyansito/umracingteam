#include <Wire.h>

Uart gpsInput(PA_3, PA_2);       // RX, TX
Uart telemetryOut(PB_11, PB_10); // RX, TX
TwoWire brakeBus(PB7, PB6);       // SDA, SCL

constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
constexpr uint8_t BRAKE_ADC_ADDRESS = 0x48;
constexpr uint16_t BRAKE_ADC_CONFIG = 0xC183;  // A0, single-shot, ±6.144 V, 128 SPS.
constexpr uint32_t BRAKE_CONVERSION_MS = 10;
constexpr float BRAKE_VOLTS_PER_COUNT = 6.144f / 32768.0f;
char gpsLine[128];
size_t gpsLineLength = 0;
uint32_t lastHeartbeatMs = 0;
uint32_t brakeConversionStartedMs = 0;
bool brakeConversionPending = false;

void startBrakeConversion() {
  brakeBus.beginTransmission(BRAKE_ADC_ADDRESS);
  brakeBus.write(uint8_t{0x01});  // Configuration register.
  brakeBus.write(static_cast<uint8_t>(BRAKE_ADC_CONFIG >> 8));
  brakeBus.write(static_cast<uint8_t>(BRAKE_ADC_CONFIG & 0xFF));
  if (brakeBus.endTransmission() != 0) {
    telemetryOut.println("BRAKE_I2C_ERROR");
    return;
  }
  brakeConversionStartedMs = millis();
  brakeConversionPending = true;
}

void reportBrakeVoltage(uint32_t now) {
  if (!brakeConversionPending || now - brakeConversionStartedMs < BRAKE_CONVERSION_MS) {
    return;
  }
  brakeConversionPending = false;

  brakeBus.beginTransmission(BRAKE_ADC_ADDRESS);
  brakeBus.write(uint8_t{0x00});  // Conversion register.
  if (brakeBus.endTransmission() != 0 ||
      brakeBus.requestFrom(BRAKE_ADC_ADDRESS, size_t{2}) != 2) {
    telemetryOut.println("BRAKE_I2C_ERROR");
    return;
  }

  const uint16_t high = brakeBus.read();
  const uint16_t low = brakeBus.read();
  const int16_t counts = static_cast<int16_t>((high << 8) | low);
  telemetryOut.print("BRAKE_V=");
  telemetryOut.println(counts * BRAKE_VOLTS_PER_COUNT, 3);
}

void setup() {
  gpsInput.begin(9600);
  telemetryOut.begin(115200);
  brakeBus.begin();
  brakeBus.setClock(100000);
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
  reportBrakeVoltage(now);
  if (now - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = now;
    telemetryOut.println("STM32_ALIVE");
    startBrakeConversion();
  }
}
