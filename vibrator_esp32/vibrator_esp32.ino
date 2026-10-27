// ESP32-C3 / ESP32-S3 ESP-NOW receiver that switches a vibrator on or off through a
// MOSFET on the battery module's B- line. Same message as ../vibrator/vibrator.ino,
// so one controller can drive both kinds of board. Wiring is in ../README.md.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

const uint8_t VIBRATOR_PIN = 4;       // GPIO4 -> 100R -> MOSFET gate
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

void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
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
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    ESP.restart();
  }
  esp_now_register_recv_cb(onReceive);
}

void loop() {
  if (vibrating && (int32_t)(millis() - stopAt) >= 0) {
    digitalWrite(VIBRATOR_PIN, LOW);
    vibrating = false;
  }
  delay(1);
}
