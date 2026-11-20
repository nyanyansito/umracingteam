#include <Wire.h>

// STM32duino 3.x renamed the concrete HardwareSerial class to Uart.
#if __has_include(<Serial.h>)
Uart gpsSerial(PA_3, PA_2);
Uart bridgeSerial(PB_11, PB_10);
#else
HardwareSerial gpsSerial(PA_3, PA_2);
HardwareSerial bridgeSerial(PB_11, PB_10);
#endif

constexpr uint8_t ADS_ADDRESS = 0x48;
constexpr uint8_t ADS_CONVERSION = 0x00;
constexpr uint8_t ADS_CONFIG = 0x01;
// Start single-shot conversion on AIN0, ±4.096 V, 128 SPS, comparator disabled.
constexpr uint16_t ADS_START = 0xC383;
constexpr uint32_t STEERING_INTERVAL_MS = 50;

bool conversionStarted = false;
uint32_t lastSteeringMs = 0;
char nmeaLine[128];
size_t nmeaLength = 0;

bool startSteeringConversion() {
  Wire.beginTransmission(ADS_ADDRESS);
  Wire.write(ADS_CONFIG);
  Wire.write(highByte(ADS_START));
  Wire.write(lowByte(ADS_START));
  return Wire.endTransmission() == 0;
}

bool readSteering(int16_t &counts) {
  Wire.beginTransmission(ADS_ADDRESS);
  Wire.write(ADS_CONVERSION);
  if (Wire.endTransmission(false) != 0 ||
      Wire.requestFrom(ADS_ADDRESS, uint8_t{2}) != 2) {
    return false;
  }

  uint16_t high = Wire.read();
  uint16_t low = Wire.read();
  counts = static_cast<int16_t>((high << 8) | low);
  return true;
}

void forwardGps() {
  while (gpsSerial.available()) {
    char next = static_cast<char>(gpsSerial.read());
    if (next == '$') {
      nmeaLine[0] = next;
      nmeaLength = 1;
    } else if (nmeaLength > 0) {
      if (next == '\n') {
        // Only forward complete sentences, so steering lines cannot split NMEA.
        bridgeSerial.write(reinterpret_cast<const uint8_t *>(nmeaLine), nmeaLength);
        bridgeSerial.write('\n');
        nmeaLength = 0;
      } else if (nmeaLength < sizeof(nmeaLine)) {
        nmeaLine[nmeaLength++] = next;
      } else {
        nmeaLength = 0;
      }
    }
  }
}

void setup() {
  bridgeSerial.begin(115200);  // USART3 TX to the ESP32
  gpsSerial.begin(9600);
  Wire.setSDA(PB7);
  Wire.setSCL(PB6);
  Wire.begin();
  Wire.setClock(100000);
  conversionStarted = startSteeringConversion();
  lastSteeringMs = millis();
}

void loop() {
  forwardGps();

  uint32_t now = millis();
  if (now - lastSteeringMs < STEERING_INTERVAL_MS) {
    return;
  }

  int16_t counts;
  bool valid = conversionStarted && readSteering(counts);
  conversionStarted = startSteeringConversion();
  lastSteeringMs = millis();  // Allow a full interval after starting conversion.

  bridgeSerial.print("UMR_STEERING,");
  if (valid) {
    bridgeSerial.print(counts);
  }
  bridgeSerial.println();
}
