// ESP32-S3 button that tells the vibrators what to do. Press GPIO8 (to GND) for a 1 s burst. Double tap
// and keep the button pressed for a continuous vibrate, until it is released. The LED on GPIO7 is on while
// the vibrators should run. The messages are the ones the vibrator sketches read, see ../vibrator/vibrator.ino.
//
// USB serial (115200) takes one command per line and logs every event with its time in ms since boot:
//   mac?            the MAC, for deploy.py
//   press <ms>      a press of the button, for tests (1-5000 ms)
//   mute <0|1>      1: send nothing, as if the button were dead
//   status          the mode and the sequence number
// The log never blocks: if the host does not read it, lines are dropped.

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
  uint8_t reserved;
  uint16_t seq;                       // counts the changes of state, so the logs can be matched
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
uint32_t virtualUntil = 0;            // end of a press from the "press" command
bool muted = false;
uint16_t seq = 0;
bool sentOn = false;                  // what the last change sent
String line;

// Logs one line, prefixed with the time. Dropped, never waited for, if the host is not reading.
void say(const String &text) {
  String out = "t=" + String(millis()) + " " + text;
  if (Serial.availableForWrite() < (int)out.length() + 2) return;
  Serial.println(out);
}

const char *modeName() {
  return mode == IDLE ? "idle" : mode == BURST ? "burst" : "hold";
}

void setMode(Mode m) {
  mode = m;
  digitalWrite(LED_PIN, m != IDLE);
  say(String("mode ") + modeName());
}

void send(bool on) {
  if (on != sentOn) {                 // a change of state gets a new sequence number
    seq++;
    sentOn = on;
    if (muted) {
      say(String("muted send seq=") + seq + " on=" + (on ? 1 : 0));
    } else {
      say(String("send seq=") + seq + " on=" + (on ? 1 : 0));
    }
  }
  if (muted) return;

  VibeCommand cmd = {MAGIC, on ? (uint8_t)1 : (uint8_t)0, 0, seq};
  // A broadcast gets no acknowledgement, so an "off" is sent three times. The vibrator ignores the repeats.
  int copies = on ? 1 : 3;
  for (int i = 0; i < copies; i++) {
    if (esp_now_send(BROADCAST, (uint8_t *)&cmd, sizeof(cmd)) != ESP_OK) {
      say("send failed");
      break;
    }
    if (i + 1 < copies) delay(5);
  }
}

void handleLine(String cmd) {
  cmd.trim();
  if (cmd == "mac?") {
    say("mac " + WiFi.macAddress());
  } else if (cmd.startsWith("press ")) {
    long ms = constrain(cmd.substring(6).toInt(), 1, 5000);
    virtualUntil = millis() + ms;
    say("cmd press " + String(ms));
  } else if (cmd.startsWith("mute ")) {
    muted = cmd.substring(5).toInt() != 0;
    say(String("mute ") + (muted ? "on" : "off"));
  } else if (cmd == "status") {
    say(String("status mode=") + modeName() + " seq=" + seq + " muted=" + (muted ? 1 : 0));
  } else {
    say("err unknown " + cmd);
  }
}

// Reads the commands without waiting for them.
void serviceSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handleLine(line);
      line = "";
    } else if (c != '\r' && line.length() < 64) {
      line += c;
    }
  }
}

void readButton(uint32_t now) {
  bool raw = digitalRead(BUTTON_PIN) == LOW || now < virtualUntil;
  if (raw != rawPressed) {
    rawPressed = raw;
    rawChanged = now;
  }
  if (now - rawChanged < DEBOUNCE_MS || raw == pressed) return;
  pressed = raw;

  if (pressed) {
    say("press");
    if (released && now - lastRelease < DOUBLE_TAP_MS) {
      setMode(HOLD);
    } else {
      setMode(BURST);
      burstEnd = now + BURST_MS;
    }
    lastSend = 0;                     // send right away
  } else {
    say("release");
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
  say("boot");
}

void loop() {
  serviceSerial();

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
