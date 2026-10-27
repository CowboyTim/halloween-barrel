// ESP-01 ESP-NOW receiver that switches a vibrator on or off through a
// MOSFET on the battery module's B- line. Wiring is in ../README.md.
//
// The button (../trigger_esp32) decides what happens; this board only follows it. The vibrator runs while
// "on" messages keep arriving, and stops on "off" or when none arrives for LEASE_MS. So a dead button, a
// lost connection or a power cut all stop it.
//
// Serial (115200) logs each change of state with its time in ms since boot and the sequence number of
// the command that caused it. "mac?" answers "mac <MAC>", for deploy.py. The log never blocks.

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
  uint8_t reserved;
  uint16_t seq;                       // counts the changes of state, so the logs can be matched
};

volatile bool wantOn = false;
volatile uint32_t lastCommand = 0;
volatile uint16_t lastSeq = 0;
bool vibrating = false;
String line;

void onReceive(uint8_t *mac, uint8_t *data, uint8_t len) {
  if (len != sizeof(VibeCommand)) return;

  VibeCommand cmd;
  memcpy(&cmd, data, sizeof(cmd));
  if (cmd.magic != MAGIC) return;

  wantOn = cmd.vibrate;
  lastSeq = cmd.seq;
  lastCommand = millis();
}

// Logs one line, prefixed with the time. Dropped, never waited for, if the host is not reading.
void say(const String &text) {
  String out = "t=" + String(millis()) + " " + text;
  if (Serial.availableForWrite() < (int)out.length() + 2) return;
  Serial.println(out);
}

void handleLine(String cmd) {
  cmd.trim();
  if (cmd == "mac?") {
    say("mac " + WiFi.macAddress());
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
  say("boot");
}

void loop() {
  serviceSerial();

  bool on = wantOn && (uint32_t)(millis() - lastCommand) < LEASE_MS;
  if (on != vibrating) {
    vibrating = on;
    digitalWrite(VIBRATOR_PIN, on ? HIGH : LOW);
    if (on) {
      say("vibrate on seq=" + String(lastSeq));
    } else {
      say("vibrate off seq=" + String(lastSeq) + (wantOn ? " reason=lease" : " reason=command"));
    }
  }
  delay(1);
}
