#include <Wire.h>

#if __has_include(<Serial.h>)
Uart bridgeSerial(PB_11, PB_10);
#else
HardwareSerial bridgeSerial(PB_11, PB_10);
#endif

constexpr uint8_t ADS_ADDRESS = 0x48;
constexpr uint8_t ADS_CONVERSION = 0x00;
constexpr uint8_t ADS_CONFIG = 0x01;
// AIN1, single-ended, ±4.096 V, single-shot, 128 SPS, comparator disabled.
constexpr uint16_t ADS_START = 0xD383;
constexpr uint32_t SAMPLE_INTERVAL_MS = 50;

bool conversionStarted = false;
uint32_t lastConversionMs = 0;

void startConversion() {
  Wire.beginTransmission(ADS_ADDRESS);
  Wire.write(ADS_CONFIG);
  Wire.write(highByte(ADS_START));
  Wire.write(lowByte(ADS_START));
  conversionStarted = Wire.endTransmission() == 0;
  lastConversionMs = millis();
}

bool readConversion(int16_t &counts) {
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

void sendSample(Print &output, bool valid, int16_t counts) {
  output.print("UMR_THROTTLE,");
  if (valid) {
    output.print(counts);
  }
  output.println();
}

void setup() {
  Serial.begin(115200);
  bridgeSerial.begin(115200);
  Wire.setSDA(PB7);
  Wire.setSCL(PB6);
  Wire.begin();
  Wire.setClock(100000);
  startConversion();
}

void loop() {
  if (millis() - lastConversionMs < SAMPLE_INTERVAL_MS) {
    return;
  }

  int16_t counts = 0;
  bool valid = conversionStarted && readConversion(counts);
  startConversion();
  if (Serial) {
    sendSample(Serial, valid, counts);
  }
  sendSample(bridgeSerial, valid, counts);
}
