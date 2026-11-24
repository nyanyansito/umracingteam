#include <Wire.h>
#include <string.h>

Uart gpsInput(PA_3, PA_2);       // RX, TX
Uart telemetryOut(PB_11, PB_10); // RX, TX
TwoWire adcBus(PB7, PB6);        // SDA, SCL

constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
constexpr bool CONFIGURE_GPS_5HZ = true;  // Requires STM32 PA2 wired to receiver RX.
constexpr uint32_t GPS_BAUD = 9600;  // Match the NEO-M8N UART1 setting.
constexpr uint32_t PEDAL_CHANNEL_SLOT_MS = 10;  // Alternate channels, giving each 50 Hz.
constexpr uint32_t ADC_CONVERSION_MS = 6;  // Allow for clock tolerance and millis() resolution.
constexpr uint8_t ADC_ADDRESS = 0x48;
constexpr uint16_t BRAKE_ADC_CONFIG = 0xC1A3;  // A0, single-shot, ±6.144 V, 250 SPS.
// A1, single-shot, ±4.096 V, 250 SPS. The dashboard uses this scale for raw counts.
constexpr uint16_t THROTTLE_ADC_CONFIG = 0xD3A3;

enum class AdcChannel { Idle, Brake, Throttle };
void reportAdcError(AdcChannel channel);
void startAdcConversion(AdcChannel channel);

// Optional 3.3 V GNSS TIMEPULSE, configured for rising-edge UTC 1 PPS.
constexpr uint32_t PPS_PIN = PB0;
volatile uint32_t pulseMs = 0;
volatile bool pulseSeen = false;
volatile bool pulsePeriodic = false;
uint32_t sentenceStartMs = 0;
uint32_t sentencePulseMs = 0;
bool sentenceHasPulse = false;

void onPulse() {
  const uint32_t now = millis();
  const uint32_t period = now - pulseMs;
  pulsePeriodic = pulseSeen && period >= 995 && period <= 1005;
  pulseSeen = true;
  pulseMs = now;
}

char gpsLine[128];
size_t gpsLineLength = 0;
uint32_t lastHeartbeatMs = 0;
uint32_t lastPedalSlotMs = 0;
uint32_t adcConversionStartedMs = 0;
AdcChannel pendingChannel = AdcChannel::Idle;
AdcChannel nextChannel = AdcChannel::Brake;

void reportAdcError(AdcChannel channel) {
  telemetryOut.print(channel == AdcChannel::Brake ? "UMR_ADC,B," : "UMR_ADC,T,");
  telemetryOut.print(millis());
  telemetryOut.println(",ERR");
}

void startAdcConversion(AdcChannel channel) {
  const uint16_t config = channel == AdcChannel::Brake ? BRAKE_ADC_CONFIG : THROTTLE_ADC_CONFIG;
  adcBus.beginTransmission(ADC_ADDRESS);
  adcBus.write(uint8_t{0x01});  // Configuration register.
  adcBus.write(static_cast<uint8_t>(config >> 8));
  adcBus.write(static_cast<uint8_t>(config & 0xFF));
  if (adcBus.endTransmission() != 0) {
    reportAdcError(channel);
    return;
  }
  adcConversionStartedMs = millis();
  pendingChannel = channel;
}

void reportAdcSample(uint32_t now) {
  if (pendingChannel == AdcChannel::Idle || now - adcConversionStartedMs < ADC_CONVERSION_MS) {
    return;
  }
  const AdcChannel channel = pendingChannel;
  pendingChannel = AdcChannel::Idle;

  adcBus.beginTransmission(ADC_ADDRESS);
  adcBus.write(uint8_t{0x00});  // Conversion register.
  if (adcBus.endTransmission() != 0 || adcBus.requestFrom(ADC_ADDRESS, size_t{2}) != 2) {
    reportAdcError(channel);
    return;
  }

  const uint16_t high = adcBus.read();
  const uint16_t low = adcBus.read();
  const int16_t counts = static_cast<int16_t>((high << 8) | low);
  telemetryOut.print(channel == AdcChannel::Brake ? "UMR_ADC,B," : "UMR_ADC,T,");
  // Estimate the midpoint of the nominal 4 ms single-shot conversion.
  telemetryOut.print(adcConversionStartedMs + 2U);
  telemetryOut.print(',');
  telemetryOut.println(counts);
}

void samplePedals(uint32_t now) {
  reportAdcSample(now);
  if (pendingChannel != AdcChannel::Idle || now - lastPedalSlotMs < PEDAL_CHANNEL_SLOT_MS) {
    return;
  }
  lastPedalSlotMs = now;
  const AdcChannel channel = nextChannel;
  nextChannel = channel == AdcChannel::Brake ? AdcChannel::Throttle : AdcChannel::Brake;
  startAdcConversion(channel);
}

// u-blox M8 volatile CFG-MSG and CFG-RATE. Leave the current UART baud in place.
void sendGpsConfig(uint8_t id, const uint8_t *payload, uint8_t length) {
  const uint8_t header[] = {0xB5, 0x62, 0x06, id, length, 0};
  uint8_t a = 0, b = 0;
  gpsInput.write(header, sizeof(header));
  for (size_t i = 2; i < sizeof(header); ++i) {
    a += header[i];
    b += a;
  }
  gpsInput.write(payload, length);
  for (size_t i = 0; i < length; ++i) {
    a += payload[i];
    b += a;
  }
  gpsInput.write(a);
  gpsInput.write(b);
}

void configureGps(uint32_t now) {
  static uint8_t step = 0;
  static uint32_t previous = 0;
  if (!CONFIGURE_GPS_5HZ || step >= 7 || now < 2000 || now - previous < 100) return;
  previous = now;
  if (step < 6) {
    // Default UART NMEA talkers GGA, GLL, GSA, GSV, RMC and VTG.
    const uint8_t message[] = {0xF0, step, static_cast<uint8_t>(step == 4 ? 1 : 0)};
    sendGpsConfig(0x01, message, sizeof(message));
  } else {
    const uint8_t rate[] = {200, 0, 1, 0, 0, 0};  // 200 ms, one fix per cycle, UTC
    sendGpsConfig(0x08, rate, sizeof(rate));
  }
  ++step;
}

void setup() {
  pinMode(PPS_PIN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PPS_PIN), onPulse, RISING);
  gpsInput.begin(GPS_BAUD);
  telemetryOut.begin(115200);
  adcBus.begin();
  adcBus.setClock(100000);
  telemetryOut.println("UMR_BOOT");
  lastHeartbeatMs = millis();
  lastPedalSlotMs = lastHeartbeatMs;
}

void loop() {
  while (gpsInput.available() > 0) {
    const char byte = static_cast<char>(gpsInput.read());
    if (byte == '$') {
      noInterrupts();
      sentenceStartMs = millis();
      sentencePulseMs = pulseMs;
      sentenceHasPulse = pulsePeriodic && sentenceStartMs - pulseMs < 1100;
      interrupts();
      gpsLine[0] = byte;
      gpsLineLength = 1;
    } else if (gpsLineLength > 0) {
      if (byte == '\n') {
        // Associate the first RMC byte with the board clock before forwarding.
        if (gpsLineLength > 6 && memcmp(gpsLine + 3, "RMC,", 4) == 0) {
          telemetryOut.print("UMR_RMC,");
          telemetryOut.print(sentenceStartMs);
          telemetryOut.print(',');
          if (sentenceHasPulse) {
            telemetryOut.print(sentencePulseMs);
          }
          telemetryOut.print(',');
        }
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
  samplePedals(now);
  configureGps(now);
}
