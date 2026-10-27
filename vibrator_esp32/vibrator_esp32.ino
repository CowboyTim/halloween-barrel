// ESP32-C3 / ESP32-S3 ESP-NOW receiver that switches a vibrator on or off through a
// MOSFET on the battery module's B- line. Same message as ../vibrator/vibrator.ino, so the
// button can drive both kinds of board. Wiring is in ../README.md.
//
// The vibrator runs while "on" messages keep arriving, and stops on "off" or when none arrives for
// LEASE_MS. So a dead button, a lost connection or a power cut all stop it.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

const uint8_t VIBRATOR_PIN = 4;       // GPIO4 -> 100R -> MOSFET gate
const uint8_t WIFI_CHANNEL = 1;       // the button must use the same channel
const uint32_t MAGIC = 0x48414C4C;    // "HALL", ignores unrelated ESP-NOW traffic
const uint32_t LEASE_MS = 500;        // the button repeats "on" every 100 ms while the vibrator should run

// Keep identical in the sender.
struct VibeCommand {
  uint32_t magic;
  uint8_t vibrate;                    // 1: on, 0: off
};

volatile bool wantOn = false;
volatile uint32_t lastCommand = 0;
bool vibrating = false;

void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(VibeCommand)) return;

  VibeCommand cmd;
  memcpy(&cmd, data, sizeof(cmd));
  if (cmd.magic != MAGIC) return;

  wantOn = cmd.vibrate;
  lastCommand = millis();
}

// deploy.py finds the board by its MAC: it sends "mac?" over serial and gets "mac <MAC>" back.
// This needs no reset, so the board keeps running.
void answerMacQuery() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line == "mac?") {
    Serial.print("mac ");
    Serial.println(WiFi.macAddress());
  }
}

void setup() {
  Serial.begin(115200);

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
  answerMacQuery();

  bool on = wantOn && (uint32_t)(millis() - lastCommand) < LEASE_MS;
  if (on != vibrating) {
    vibrating = on;
    digitalWrite(VIBRATOR_PIN, on ? HIGH : LOW);
    Serial.println(on ? "vibrate on" : "vibrate off");
  }
  delay(1);
}
