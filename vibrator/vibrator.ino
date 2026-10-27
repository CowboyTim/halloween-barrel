// ESP-01 ESP-NOW receiver that switches a vibrator on or off through a
// MOSFET on the battery module's B- line. Wiring is in ../README.md.
//
// The button (../trigger_esp32) decides what happens; this board only follows it. The vibrator runs while
// "on" messages keep arriving, and stops on "off" or when none arrives for LEASE_MS. So a dead button, a
// lost connection or a power cut all stop it.

#include <ESP8266WiFi.h>
#include <espnow.h>

const uint8_t VIBRATOR_PIN = 2;       // GPIO2 -> 100R -> MOSFET gate
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

void onReceive(uint8_t *mac, uint8_t *data, uint8_t len) {
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
  wifi_set_channel(WIFI_CHANNEL);

  if (esp_now_init() != 0) {
    ESP.restart();
  }
  esp_now_set_self_role(ESP_NOW_ROLE_SLAVE);
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
