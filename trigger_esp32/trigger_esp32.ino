// ESP32-S3 button that tells the vibrators what to do. Press GPIO8 (to GND) for a 1 s burst. Double tap
// and keep the button pressed for a continuous vibrate, until it is released. The LED on GPIO7 is on while
// the vibrators should run. The messages are the ones the vibrator sketches read, see ../vibrator/vibrator.ino.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

const uint8_t BUTTON_PIN = 8;         // pressed = LOW
const uint8_t LED_PIN = 7;
const uint8_t WIFI_CHANNEL = 1;       // the vibrators listen on this channel
const uint32_t MAGIC = 0x48414C4C;    // "HALL"
const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
const uint32_t BURST_MS = 1000;       // one press
const uint32_t DOUBLE_TAP_MS = 350;   // a press this soon after a release is a double tap
const uint32_t DEBOUNCE_MS = 20;
const uint32_t HEARTBEAT_MS = 100;    // "on" is repeated while the vibrators should run

// Keep identical in the receivers.
struct VibeCommand {
  uint32_t magic;
  uint8_t vibrate;                    // 1: on, 0: off
};

enum Mode { IDLE, BURST, HOLD };
Mode mode = IDLE;
bool pressed = false;                 // debounced
bool rawPressed = false;
uint32_t rawChanged = 0;
bool released = false;                // true once the button has been released after boot
uint32_t lastRelease = 0;
uint32_t burstEnd = 0;
uint32_t lastSend = 0;

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

void send(bool on) {
  VibeCommand cmd = {MAGIC, on ? (uint8_t)1 : (uint8_t)0};
  if (esp_now_send(BROADCAST, (uint8_t *)&cmd, sizeof(cmd)) != ESP_OK) {
    Serial.println("send failed");
  }
}

void setMode(Mode m) {
  mode = m;
  digitalWrite(LED_PIN, m != IDLE);
  Serial.printf("mode %s\n", m == IDLE ? "idle" : m == BURST ? "burst" : "hold");
}

void readButton(uint32_t now) {
  bool raw = digitalRead(BUTTON_PIN) == LOW;
  if (raw != rawPressed) {
    rawPressed = raw;
    rawChanged = now;
  }
  if (now - rawChanged < DEBOUNCE_MS || raw == pressed) return;
  pressed = raw;

  if (pressed) {
    if (released && now - lastRelease < DOUBLE_TAP_MS) {
      setMode(HOLD);
    } else {
      setMode(BURST);
      burstEnd = now + BURST_MS;
    }
    lastSend = 0;                     // send right away
  } else {
    released = true;
    lastRelease = now;
    if (mode == HOLD) {
      setMode(IDLE);
      send(false);
    }
  }
}

void setup() {
  Serial.begin(115200);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    ESP.restart();
  }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, sizeof(BROADCAST));
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

void loop() {
  answerMacQuery();

  uint32_t now = millis();
  readButton(now);

  if (mode == BURST && now >= burstEnd) {
    setMode(IDLE);
    send(false);
  }
  if (mode != IDLE && now - lastSend >= HEARTBEAT_MS) {
    lastSend = now;
    send(true);
  }
  delay(2);
}
