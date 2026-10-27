# Halloween barrel: ESP-NOW vibrator

An ESP-01 receives ESP-NOW commands and switches a vibrator that runs from a
5V step-up / charger module (Otronic OT2069-B49, "5V step-up power and charging
module for lithium battery") on a single 3.7V cell. A button (ESP32-S3) decides
when the vibrator runs.

Schematic: [docs/schematic.svg](docs/schematic.svg).

## Layout

- `vibrator/vibrator.ino`: Arduino sketch for the ESP-01 (ESP8266 core).
- `vibrator_esp32/vibrator_esp32.ino`: the same receiver for ESP32-C3 / ESP32-S3 boards
  (gate on GPIO4).
- `trigger_esp32/trigger_esp32.ino`: the button (GPIO8) and LED (GPIO7) on an ESP32-S3;
  sends the vibrate commands.
- `docs/schematic.svg`: wiring schematic.
- `docs/programmer.svg`: the CH340E programmer for the ESP-01 (DTR reset, GPIO0 button, bulk cap).
- `docs/led-test.svg`: the MOSFET module switching an LED, for a bench check before the vibrator.
- `docs/ESP-01-ESP8266-pinout-gpio-pin.png`: the ESP-01 pinout.
- `deploy.py`: builds, flashes and monitors the boards listed in `boards.toml`.
- `boards.toml`: one entry per board (port, MAC, chip, role).

## Parts

- ESP-01 (ESP8266), or an ESP32-C3 / ESP32-S3 board instead. A 3.3 V regulator
  that accepts the cell voltage can replace the LDO in that case.
- Otronic OT2069-B49 charger / 5V boost module (input 3.7–5.5 V, output 5 V, max 1 A)
- 3.7 V Li-ion / LiPo cell with its own protection circuit (e.g. a 18650 from a
  pack or a LiPo pouch). Make sure the cell is intact and protected.
- 3.3 V LDO that accepts a 3.7–4.2 V input and delivers ≥ 300 mA, e.g. AP2112K-3.3
  (600 mA, SOT-23-5). An AMS1117 won't work: its ~1.1 V dropout starves the ESP-01
  once the cell is below ~4.4 V.
- MOSFET module 5–36 V, 15 A (TinyTronics SKU 001958). It switches the motor with
  its MOSFET (marked D4184 on the board; check the marking). The Trig input accepts
  3.3 V.
- 10 µF + 100 nF close to the ESP-01 supply pins, 1 µF in and out of the LDO.
- Inline fuse on battery+ (1–2 A).
- Flyback diode across the vibrator if the MOSFET module has none, and the vibrator
  is a DC motor (1N5819 or similar).

## Wiring

The MOSFET module switches the boost's 5 V output to the vibrator, so the boost
keeps running (8 µA standby). The ESP-01 runs from the battery through the LDO and
stays on to listen for ESP-NOW.

```
Battery+ ──[fuse 1–2 A]──┬── LDO VIN   (1 µF)
                         └── OT2069 B+

LDO VOUT (3.3 V) ── ESP-01 VCC   (10 µF + 100 nF at the pins)
ESP-01 GPIO2 ─────── MOSFET module Trig
ESP-01 GND, LDO GND, module GND ── Battery-

OT2069 OUT+ (5 V) ── module Vin+
OT2069 OUT- ──────── Battery-   (same ground)
module Vin- ──────── Battery-
module Vout+ ─────── vibrator +
module Vout- ─────── vibrator -
(flyback diode across the vibrator, if the module has none)
```

Notes:

- The ESP-01 needs `CH_PD` and `RST` tied high (3.3 V). Most ESP-01 boards have
  pull-ups on these already.
- GPIO2 is high while the ESP-01 boots, so the vibrator may pulse at power-up unless
  the MOSFET module's Trig input has a pulldown. Check that with a meter.
- The Otronic listing does not label B+/B- or the output connector. Check the
  board silkscreen and confirm with a meter that B- and the 5 V output ground are
  the same net, since the MOSFET module's Vin- and the ESP-01 GND go there.
- The board is silkscreened ST6845-C. No datasheet for that marking turned up.
  Listings for the similar T6845-C board describe a USB-A output, a Micro-B
  charge input, B+/B- battery pads, and a 2.9 V discharge cutoff. Confirm the pad
  labels and the common ground with a meter before wiring.

## Power budget

ESP-NOW reception keeps the radio on, which draws roughly 60–80 mA continuously.
A 2000 mAh cell lasts about a day. Modem or light sleep isn't compatible with
reliable ESP-NOW reception, so this design trades battery life for instant
response. Deep sleep with a timer wake would save power but drops commands
between wakes.

The cell should not go below ~3.4 V under load. The LDO and ESP-01 brown out
before the module's own discharge cutoff.

## Flashing the ESP-01

Use a 3.3 V USB-serial adapter, or disconnect the battery and feed 3.3 V at
the ESP-01 VCC pin. Then:

- `GPIO0` to GND during flash, released afterwards
- `RX` to adapter TX, `TX` to adapter RX
- `RST` and `CH_PD` high

Board: "Generic ESP8266 Module" (`esp8266:esp8266:generic`), 1 MB flash.

Put the board's port in `boards.toml` (`python3 deploy.py list` shows the ports),
then:

```
python3 deploy.py update                 # once: installs the arduino cores
python3 deploy.py build vibrator
python3 deploy.py flash vibrator         # GPIO0 low while it resets
python3 deploy.py monitor vibrator
```

## Protocol

Send a `VibeCommand` (magic `0x48414C4C`, `vibrate` 1 for on, 0 for off, `seq` counting the changes of state) over
ESP-NOW on WiFi channel 1. Sender and receiver must match on the channel and
on the struct layout. The receiver ignores frames with the wrong size or magic.
Both sketches accept the same message, so one sender can drive ESP-01 and ESP32 boards.

The button (`trigger_esp32`) decides what happens. A press gives a 1 s burst. A
double tap, with the button kept pressed, vibrates until it is released. While the
vibrator should run, the button sends `on` every 100 ms, and `off` when the burst ends
or the button is released. A vibrator stops when no `on` arrives for 500 ms, so a
dead button, a lost connection or a power cut stop it too.

Serial: send `mac?` on 115200 baud and the board answers `mac <MAC>`. `deploy.py`
uses this to find a board by its MAC on whatever port it is on, without resetting
it. A board that still runs firmware without this answer has to be flashed once in
download mode (GPIO0 low while it resets), with the port given explicitly:
`python3 deploy.py flash vibrator --port /dev/ttyUSB0 --no-reset`.

## Not verified yet

- The MOSFET module's D4184 is not confirmed. The closest published part is a 40 V
  N-channel MOSFET, not a datasheet for this marking.
- The Trig pulldown and the power-up behavior of the module (see Notes).

## Logs and tests

Both boards log over USB serial at 115200 baud, one line per event, each prefixed
with the time in ms since boot:

- Button (`trigger_esp32`): `press`, `release`, `mode burst|hold|idle`,
  `send seq=N on=0|1`, `muted send ...`, `boot`
- Vibrator (`vibrator`): `vibrate on seq=N`, `vibrate off seq=N reason=command|lease`,
  `boot`

The button also takes commands on its serial port: `press <ms>` (a press from the
port), `mute 0|1` (1 sends nothing, as if the button were dead), `status`, and `mac?`.

`tests/test_button.py` drives the button over its port and checks both logs. It
covers a single press as one 1 s burst, presses right after a burst, a double tap
held until release, the vibrator stopping when the button goes silent, the
press-to-vibrate latency (median about 100 ms), and matching sequence numbers on
both sides.

```
python3 tests/test_button.py -v
```

The suite needs both boards answering on USB. Don't run `deploy.py monitor` while
it runs, since the monitor shares the ports. Each "off" is sent three times, since a
broadcast isn't acknowledged. The vibrator's lease stops it if all three are lost.

## Ideas

- **Automatic trigger:** a PIR motion sensor on the trigger board (the ESP32-S3)
  could send the same messages as the button, with no change on the vibrator side.
  Each motion would start an "on" stream that stops after a few seconds, and the
  vibrators stop on their own when the messages stop.
