#!/usr/bin/env python3
"""Tests for the button and the vibrator, over their USB serial ports.

Needs the button (ESP32-S3) and the vibrator (ESP-01) on USB, both answering (see 'deploy.py list').
The button is driven over its serial port ("press <ms>", "mute <0|1>", "status"). Both boards log their
events with a time in ms, and the tests check the two logs against each other, by sequence number and by
the host's clock.

  python3 -m unittest tests/test_button.py -v
"""

import os
import re
import statistics
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import deploy  # noqa: E402

BURST_S = 1.0       # a single press: one burst of this length
IDLE_S = 1.5        # between tests, so each test starts from idle
MAX_LATENCY_S = 0.15


class Device:
    """A board on a serial port: a reader thread keeps every line with the host's time."""

    def __init__(self, port):
        self.s = deploy.open_quiet(port)
        self.log = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            try:
                data = self.s.read(256)
            except Exception:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with self.lock:
                    self.log.append((time.monotonic(), line.decode("utf-8", "replace").strip()))

    def send(self, text):
        t = time.monotonic()
        self.s.write((text + "\n").encode())
        return t

    def wait_for(self, pattern, timeout=3.0, after=0.0):
        """The first line at or after 'after' that matches: (host time, match), or (None, None)."""
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        while True:
            with self.lock:
                for t, text in self.log:
                    if t >= after:
                        m = rx.search(text)
                        if m:
                            return t, m
            if time.monotonic() > end:
                return None, None
            time.sleep(0.01)

    def lines_between(self, start, end, pattern):
        rx = re.compile(pattern)
        with self.lock:
            return [text for t, text in self.log if start <= t <= end and rx.search(text)]

    def close(self):
        self.s.close()


class ButtonTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        boards = deploy.load_boards()
        trigger_port = deploy.locate(boards["button"])
        vib_port = deploy.locate(boards["vibrator"])
        if not trigger_port or not vib_port:
            raise unittest.SkipTest("both boards must answer on USB (see 'deploy.py list')")
        cls.trigger = Device(trigger_port)
        cls.vib = Device(vib_port)
        time.sleep(3)      # opening the button's port resets it: wait for it to boot
        cls.trigger.send("mute 0")
        if cls.trigger.wait_for(r"mute off", 5)[0] is None:
            raise RuntimeError("the button does not answer on " + trigger_port)

    @classmethod
    def tearDownClass(cls):
        cls.trigger.send("mute 0")
        time.sleep(0.5)
        cls.trigger.close()
        cls.vib.close()

    def setUp(self):
        time.sleep(IDLE_S)

    def press(self, ms):
        """A press from the serial port, and the vibrator's on and off lines for it."""
        t_cmd = self.trigger.send(f"press {ms}")
        t_on, m_on = self.vib.wait_for(r"vibrate on seq=(\d+)", 2, after=t_cmd)
        self.assertIsNotNone(t_on, "the vibrator did not turn on")
        t_off, _ = self.vib.wait_for(r"vibrate off seq=\d+", ms / 1000 + 3, after=t_on)
        self.assertIsNotNone(t_off, "the vibrator did not turn off")
        return t_cmd, t_on, t_off, m_on

    def test_01_both_boards_answer_their_mac(self):
        boards = deploy.load_boards()
        for dev, name in ((self.trigger, "button"), (self.vib, "vibrator")):
            dev.send("mac?")
            t, m = dev.wait_for(r"mac ((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", 3)
            self.assertIsNotNone(t, f"{name} did not answer mac?")
            self.assertEqual(m.group(1).lower(), boards[name]["mac"])

    def test_02_single_press_is_one_burst(self):
        t_cmd, t_on, t_off, m_on = self.press(50)
        self.assertLess(t_on - t_cmd, MAX_LATENCY_S, "press to vibrate on too slow")
        duration = t_off - t_on
        self.assertTrue(BURST_S - 0.1 <= duration <= BURST_S + 0.2, f"burst lasted {duration:.2f} s")
        # the button and the vibrator logged the same change of state
        t_s, m_s = self.trigger.wait_for(r"send seq=(\d+) on=1", 2, after=t_cmd)
        self.assertIsNotNone(t_s, "the button did not log the send")
        self.assertEqual(m_s.group(1), m_on.group(1), "sequence numbers differ between the two logs")

    def test_03_presses_work_right_after_a_burst(self):
        for i in range(3):
            t_cmd, t_on, _, _ = self.press(50)
            self.assertLess(t_on - t_cmd, MAX_LATENCY_S, f"press {i + 1} too slow")
            time.sleep(0.3)        # right after the burst, not after a long idle

    def test_04_double_tap_holds_until_release(self):
        t_first = self.trigger.send("press 50")
        time.sleep(0.15)
        self.trigger.send("press 2500")
        t_on, _ = self.vib.wait_for(r"vibrate on", 2, after=t_first)
        self.assertIsNotNone(t_on)
        t_off, m_off = self.vib.wait_for(r"vibrate off seq=\d+ reason=(command|lease)", 6, after=t_on)
        self.assertIsNotNone(t_off, "the vibrator did not stop on release")
        if m_off.group(1) == "lease":
            print("\nthe release was not received; the lease stopped the vibrator")
        duration = t_off - t_on
        self.assertTrue(2.4 <= duration <= 3.0, f"hold lasted {duration:.2f} s, expected about 2.65 s")
        stops = self.vib.lines_between(t_on, t_off - 0.001, r"vibrate off")
        self.assertEqual(stops, [], "the vibrator stopped during the hold")

    def test_05_vibrator_stops_when_the_button_goes_silent(self):
        self.trigger.send("press 50")
        time.sleep(0.15)
        self.trigger.send("press 4000")          # a hold
        t_on, _ = self.vib.wait_for(r"vibrate on", 2, after=time.monotonic() - 1)
        self.assertIsNotNone(t_on)
        time.sleep(0.5)
        t_mute = self.trigger.send("mute 1")     # the button goes silent mid-hold
        t_off, m_off = self.vib.wait_for(r"vibrate off seq=\d+ reason=lease", 3, after=t_mute)
        self.assertIsNotNone(t_off, "the vibrator kept running without the button")
        self.assertLess(t_off - t_mute, 0.8, "stop after the silence too slow")
        self.trigger.send("mute 0")
        self.trigger.wait_for(r"mute off", 3)
        time.sleep(4)                            # the held press ends; let the button go idle

    def test_06_press_to_vibrate_latency(self):
        latencies = []
        for _ in range(10):
            t_cmd, t_on, _, _ = self.press(50)
            latencies.append(t_on - t_cmd)
            time.sleep(0.3)
        med = statistics.median(latencies)
        print(f"\npress to vibrate on: median {med * 1000:.0f} ms, max {max(latencies) * 1000:.0f} ms")
        self.assertLess(med, MAX_LATENCY_S)

    def test_07_both_sides_log_with_time(self):
        t_cmd = self.trigger.send("status")
        t, m = self.trigger.wait_for(r"status mode=(\w+)", 2, after=t_cmd)
        self.assertIsNotNone(t, "the button did not answer status")
        self.assertEqual(m.group(1), "idle")
        self.assertTrue(any(re.search(r"^t=\d+ ", text) for _, text in self.trigger.log[-20:]))
        self.assertTrue(any(re.search(r"^t=\d+ ", text) for _, text in self.vib.log[-20:]))


if __name__ == "__main__":
    unittest.main()
