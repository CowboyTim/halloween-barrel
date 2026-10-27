# Halloween barrel: ESP-NOW vibrator

An ESP-01 receives ESP-NOW commands and switches a vibrator that runs from a
5V step-up / charger module (Otronic OT2069-B49, "5V step-up power and charging
module for lithium battery") on a single 3.7V cell.

## Layout

- `vibrator/vibrator.ino`: Arduino sketch for the ESP-01 (ESP8266 core).

## Parts

- ESP-01 (ESP8266)
- Otronic OT2069-B49 charger / 5V boost module (input 3.7–5.5 V, output 5 V, max 1 A)
- 3.7 V Li-ion / LiPo cell with its own protection circuit (e.g. a 18650 from a
  pack or a LiPo pouch). Make sure the cell is intact and protected.
- 3.3 V LDO that accepts a 3.7–4.2 V input and delivers ≥ 300 mA, e.g. AP2112K-3.3
  (600 mA, SOT-23-5). An AMS1117 won't work: its ~1.1 V dropout starves the ESP-01
  once the cell is below ~4.4 V.
- Logic-level N-MOSFET, e.g. IRLML6344 or AO3400A (SOT-23), rated ≥ 20 V.
- 100 Ω gate resistor, 10 kΩ gate pulldown.
- 10 µF + 100 nF close to the ESP-01 supply pins, 1 µF in and out of the LDO.
- Inline fuse on battery+ (1–2 A).
- If the vibrator is a bare DC motor: flyback diode across it (1N5819 or 1N4148).

## Wiring

The MOSFET sits on the module's **B-** (low side). Cutting B- turns the boost
off completely, so the module's standby draw goes to near zero. The ESP-01 must
**not** be powered from the switched side. It runs from the battery directly
through the LDO and stays on to listen for ESP-NOW.

```
Battery+ ──[fuse]──┬── LDO VIN
                   │   LDO VOUT (3.3 V) ── ESP-01 VCC
                   │                      (10 µF + 100 nF at the pins)
                   └── Module B+

Battery- ──┬── LDO GND
           ├── ESP-01 GND
           └── MOSFET source

Module B- ──── MOSFET drain
MOSFET gate ── 100 Ω ── ESP-01 GPIO2
MOSFET gate ── 10 kΩ ── GND

Module OUT+ (5 V) ── vibrator +
Module OUT- (GND) ── vibrator -
```

Notes:

- The ESP-01 needs `CH_PD` and `RST` tied high (3.3 V). Most ESP-01 boards have
  pull-ups on these already.
- The Otronic listing does not label B+/B- or the output connector. Check the
  board silkscreen and confirm with a meter that B- and the 5 V output ground are
  the same net. If they are not, the MOSFET has to go on the output side instead.
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

Board: "Generic ESP8266 Module" (`esp8266:esp8266:generic`), flash mode
DIO, 1 MB flash.

```
arduino-cli compile --fqbn esp8266:esp8266:generic vibrator
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp8266:esp8266:generic vibrator
```

## Protocol

Send a `VibeCommand` (magic `0x48414C4C`, `durationMs` up to 10000) over
ESP-NOW on WiFi channel 1. Sender and receiver must match on the channel and
on the struct layout. The receiver ignores frames with the wrong size or magic.

## Not verified yet

- The sketch has not been run on hardware.
- Behavior of the MOSFET stage with the actual vibrator is untested.
- The gate pulldown should hold the MOSFET off during ESP-01 boot. GPIO2 is
  high at boot, so measure the gate voltage at power-up before trusting it.
