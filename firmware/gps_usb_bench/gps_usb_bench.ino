#if !defined(USBD_USE_CDC) || defined(DISABLE_GENERIC_SERIALUSB)
#error "Select USB support: CDC (generic Serial supersede U(S)ART)."
#endif

// STM32duino 3.x renamed the concrete HardwareSerial class to Uart.
#if __has_include(<Serial.h>)
Uart gpsSerial(PA_3, PA_2);
#else
HardwareSerial gpsSerial(PA_3, PA_2);
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
        Serial.write(reinterpret_cast<const uint8_t *>(nmeaLine), nmeaLength);
        Serial.write('\n');
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
  Serial.begin(115200);  // USB CDC, to the laptop
  gpsSerial.begin(9600);
}

void loop() {
  forwardGps();
}
