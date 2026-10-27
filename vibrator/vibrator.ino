// ESP-01 ESP-NOW receiver that switches a vibrator on or off through a
// MOSFET on the battery module's B- line. Wiring is in ../README.md.

#include <ESP8266WiFi.h>
#include <espnow.h>

const uint8_t VIBRATOR_PIN = 2;       // GPIO2 -> 100R -> MOSFET gate
const uint8_t WIFI_CHANNEL = 1;       // the sender must use the same channel
const uint32_t MAGIC = 0x48414C4C;    // "HALL", ignores unrelated ESP-NOW traffic
const uint16_t MAX_DURATION_MS = 10000;

// Keep identical in the sender.
struct VibeCommand {
  uint32_t magic;
  uint16_t durationMs;
};

volatile bool vibrating = false;
volatile uint32_t stopAt = 0;

void onReceive(uint8_t *mac, uint8_t *data, uint8_t len) {
  if (len != sizeof(VibeCommand)) return;

  VibeCommand cmd;
  memcpy(&cmd, data, sizeof(cmd));
  if (cmd.magic != MAGIC) return;

  stopAt = millis() + min(cmd.durationMs, MAX_DURATION_MS);
  vibrating = true;
  digitalWrite(VIBRATOR_PIN, HIGH);
}

void setup() {
  // Keep the boost module off until a command arrives.
  pinMode(VIBRATOR_PIN, OUTPUT);
  digitalWrite(VIBRATOR_PIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  wifi_set_channel(WIFI_CHANNEL);

  if (esp_now_init() != 0) {
    ESP.restart();
  }
  esp_now_set_self_role(ESP_NOW_ROLE_SLAVE);
  esp_now_register_recv_cb(onReceive);
}

void loop() {
  if (vibrating && (int32_t)(millis() - stopAt) >= 0) {
    digitalWrite(VIBRATOR_PIN, LOW);
    vibrating = false;
  }
  delay(1);
}
