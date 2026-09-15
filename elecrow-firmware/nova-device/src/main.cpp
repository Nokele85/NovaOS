#include <Arduino.h>

// Safe first probe: no board-specific camera/display pins are touched.
// The PC can use this serial heartbeat to verify USB + firmware deployment.
static constexpr uint32_t BAUD = 115200;
static constexpr uint32_t HEARTBEAT_MS = 2000;
uint32_t last_heartbeat = 0;

void setup() {
  Serial.begin(BAUD);
  delay(300);
  Serial.println("NOVA_ELECROW_BOOT");
  Serial.println("board=ESP32-S3R8");
  Serial.println("probe=safe-serial-heartbeat");
  Serial.println("camera/display/audio=not-yet-enabled");
}

void loop() {
  if (millis() - last_heartbeat >= HEARTBEAT_MS) {
    last_heartbeat = millis();
    Serial.printf("NOVA_HEARTBEAT uptime_ms=%lu\n", (unsigned long)millis());
  }
  if (Serial.available()) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    if (command == "ping") Serial.println("NOVA_PONG");
    else if (command == "status") Serial.println("NOVA_STATUS safe-probe-ready");
    else Serial.println("NOVA_UNKNOWN_COMMAND");
  }
}
