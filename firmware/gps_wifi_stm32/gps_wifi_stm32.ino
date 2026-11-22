// STM32duino 3.x renamed the concrete HardwareSerial class to Uart.
#if __has_include(<Serial.h>)
Uart gpsSerial(PA_3, PA_2);
Uart bridgeSerial(PB_11, PB_10);
#else
HardwareSerial gpsSerial(PA_3, PA_2);
HardwareSerial bridgeSerial(PB_11, PB_10);
#endif

char nmeaLine[128];
size_t nmeaLength = 0;

void forwardGps() {
  while (gpsSerial.available()) {
    char next = static_cast<char>(gpsSerial.read());
    if (next == '$') {
      nmeaLine[0] = next;
      nmeaLength = 1;
    } else if (nmeaLength > 0) {
      if (next == '\n') {
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
}

void loop() {
  forwardGps();
}
