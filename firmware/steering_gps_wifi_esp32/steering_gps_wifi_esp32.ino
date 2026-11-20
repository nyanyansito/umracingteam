#include <WiFi.h>

constexpr char WIFI_SSID[] = "UMR-GPS-Test";
constexpr char WIFI_PASSWORD[] = "UMR-Bench-2026";
constexpr int STM32_RX_PIN = 18;
constexpr uint16_t TCP_PORT = 9000;
constexpr size_t UART_READ_BUDGET = 512;

HardwareSerial stm32Uart(1);
WiFiServer server(TCP_PORT);
WiFiClient client;
uint8_t line[160];
size_t lineLength = 0;
bool discardLine = true;

void setup() {
  Serial.begin(115200);
  stm32Uart.begin(115200, SERIAL_8N1, STM32_RX_PIN, -1);

  const IPAddress address(192, 168, 4, 1);
  WiFi.mode(WIFI_AP);
  if (!WiFi.softAPConfig(address, address, IPAddress(255, 255, 255, 0)) ||
      !WiFi.softAP(WIFI_SSID, WIFI_PASSWORD)) {
    Serial.println("UMR Wi-Fi access point failed to start");
    return;
  }
  server.begin();
  Serial.println("UMR telemetry ready at 192.168.4.1:9000");
}

void loop() {
  WiFiClient incoming = server.accept();
  if (incoming) {
    client.stop();
    client = incoming;
    client.setNoDelay(true);
    lineLength = 0;
    discardLine = true;
  }

  // Sync to a newline after a laptop reconnect or an oversized UART line.
  for (size_t count = 0; count < UART_READ_BUDGET && stm32Uart.available(); ++count) {
    const int next = stm32Uart.read();
    if (next < 0) {
      break;
    }
    if (!discardLine && lineLength < sizeof(line)) {
      line[lineLength++] = static_cast<uint8_t>(next);
    } else {
      discardLine = true;
    }

    if (next == '\n') {
      if (!discardLine && client.connected() &&
          client.write(line, lineLength) != lineLength) {
        client.stop();
      }
      lineLength = 0;
      discardLine = false;
    }
  }
  delay(1);
}
