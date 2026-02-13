#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import struct
import time
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
from pathlib import Path

import warnings
warnings.filterwarnings(
    "ignore",
    message="Unable to import Axes3D"
)

# ---- Protocol constants ----
OUTBOUND_PREFIX = b">"   # device -> host marker
INBOUND_PREFIX = b"<"    # host -> device marker

CMD_DEVICE_QUERY = 22
CMD_APP_START = 1
CMD_SET_RADIO_PARAMS = 11
CMD_GET_STATS = 56

RESP_OK = 0
RESP_ERR = 1
RESP_DEVICE_INFO = 13
RESP_SELF_INFO = 5
RESP_STATS = 24

STATS_RADIO = 1  # (may vary by firmware)


class MeshCoreDisconnected(Exception):
    pass


@dataclass
class RadioStats:
    noise_floor: int
    last_rssi: int
    last_snr_x4: int
    tx_air_time_s: int
    rx_air_time_s: int


# Optional serial support
try:
    import serial
except ImportError:
    serial = None


# ----------------------------
# Transport Layer
# ----------------------------

class TransportClosed(Exception):
    pass


class BaseTransport:
    async def open(self):
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError

    async def write(self, data: bytes):
        raise NotImplementedError

    async def read_exactly(self, n: int, timeout: float) -> bytes:
        raise NotImplementedError


class TCPTransport(BaseTransport):
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def open(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            self.reader = None

    async def write(self, data: bytes):
        if not self.writer:
            raise TransportClosed
        self.writer.write(data)
        await self.writer.drain()

    async def read_exactly(self, n: int, timeout: float) -> bytes:
        if not self.reader:
            raise TransportClosed
        return await asyncio.wait_for(self.reader.readexactly(n), timeout)


class USBTransport(BaseTransport):
    def __init__(self, device: str, baud: int = 115200):
        if serial is None:
            raise RuntimeError("pyserial not installed (pip install pyserial)")
        self.device = device
        self.baud = baud
        self.ser = None

    async def open(self):
        # Non-blocking reads; we implement our own timeout loop
        self.ser = serial.Serial(self.device, self.baud, timeout=0)

    async def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    async def write(self, data: bytes):
        if not self.ser:
            raise TransportClosed
        self.ser.write(data)
        self.ser.flush()

    async def read_exactly(self, n: int, timeout: float) -> bytes:
        if not self.ser:
            raise TransportClosed

        end = time.monotonic() + timeout
        buf = bytearray()

        while len(buf) < n:
            if time.monotonic() > end:
                raise TimeoutError("Serial read timeout")
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                await asyncio.sleep(0.01)

        return bytes(buf)


# ----------------------------
# Companion Client
# ----------------------------

class MeshCoreCompanionClient:
    def __init__(self, transport: BaseTransport, *, timeout_s: float = 10.0, debug: bool = False):
        self.t = transport
        self.timeout_s = timeout_s
        self.debug = debug

        # Cache the exact GET_STATS request shape that works on this firmware
        self._get_stats_shape: Optional[bytes] = None

    async def connect(self):
        await self.t.open()

    async def close(self):
        await self.t.close()

    async def _send(self, payload: bytes):
        pkt = INBOUND_PREFIX + struct.pack("<H", len(payload)) + payload
        if self.debug:
            print(f"[TX] {pkt.hex()}")
        await self.t.write(pkt)

    async def _recv(self):
        while True:
            b = await self.t.read_exactly(1, self.timeout_s)
            if b == OUTBOUND_PREFIX:
                break

        length = struct.unpack("<H", await self.t.read_exactly(2, self.timeout_s))[0]
        payload = await self.t.read_exactly(length, self.timeout_s)

        if self.debug:
            print(f"[RX] {payload.hex()}")

        return payload

    async def _req(self, payload: bytes, expect):
        await self._send(payload)
        while True:
            r = await self._recv()
            if r and r[0] in expect:
                return r

    async def handshake(self, app_name="NoiseFloorScanner"):
        # Same handshake method as your meshcore_connect example
        await self._req(bytes([CMD_DEVICE_QUERY, 7]), (RESP_DEVICE_INFO,))
        await self._req(
            bytes([CMD_APP_START, 7]) + b"\x00" * 6 + app_name.encode(),
            (RESP_SELF_INFO,),
        )

    async def set_radio(self, freq_mhz, bw_khz, sf, cr):
        payload = bytes([CMD_SET_RADIO_PARAMS]) + struct.pack(
            "<IIbb",
            int(freq_mhz * 1000),
            int(bw_khz * 1000),
            sf,
            cr,
        )
        r = await self._req(payload, (RESP_OK, RESP_ERR))
        if r[0] == RESP_ERR:
            code = r[1] if len(r) > 1 else None
            raise RuntimeError(f"SET_RADIO_PARAMS failed (RESP_ERR code={code})")

    async def get_noise_floor(self) -> int:
        """
        Firmware variants differ in GET_STATS request format.
        Try a few known shapes (including the common '7' protocol byte),
        cache the one that works, and then reuse it.
        """

        async def try_payload(payload: bytes):
            return await self._req(payload, (RESP_STATS, RESP_ERR))

        # If we already found a working payload shape, use it
        if self._get_stats_shape is not None:
            r = await try_payload(self._get_stats_shape)
            if r and r[0] == RESP_STATS:
                return struct.unpack_from("<h", r, 2)[0]
            # stopped working -> re-detect
            self._get_stats_shape = None

        groups = [1, 0, 2, 3, 4, 5]

        # Candidate request payloads:
        #  - old: [56, group]
        #  - some builds: [56, 7, group]
        #  - some builds: [56, 7, group, 0] or [56, 7, group, 0, 0]
        #  - some builds: [56, group, 0] / [56, group, 0, 0]
        candidates: list[bytes] = []
        for g in groups:
            candidates.append(bytes([CMD_GET_STATS, g]))                 # 2 bytes
            candidates.append(bytes([CMD_GET_STATS, 7, g]))              # 3 bytes (adds 7)
            candidates.append(bytes([CMD_GET_STATS, 7, g, 0]))           # 4 bytes
            candidates.append(bytes([CMD_GET_STATS, 7, g, 0, 0]))        # 5 bytes
            candidates.append(bytes([CMD_GET_STATS, g, 0]))              # 3 bytes alt
            candidates.append(bytes([CMD_GET_STATS, g, 0, 0]))           # 4 bytes alt

        last_code = None
        for payload in candidates:
            r = await try_payload(payload)
            if r[0] == RESP_STATS:
                self._get_stats_shape = payload
                if self.debug:
                    print(f"[+] GET_STATS working payload: {payload.hex()}")
                return struct.unpack_from("<h", r, 2)[0]

            last_code = r[1] if len(r) > 1 else None
            if self.debug:
                print(f"[!] GET_STATS payload={payload.hex()} RESP_ERR code={last_code}")

        raise RuntimeError(f"GET_STATS failed for all payload shapes (last RESP_ERR code={last_code})")


# ----------------------------
# Measurement Logic (UNCHANGED)
# ----------------------------

def frange(start, stop, step):
    f = start
    while f <= stop + 1e-9:
        yield round(f, 6)
        f += step


async def measure_freq(client, freq, args):
    await client.set_radio(freq, args.bw_khz, args.sf, args.cr)
    await asyncio.sleep(args.settle_s)

    samples = []
    end = time.time() + args.dwell_min * 60

    while time.time() < end:
        nf = await client.get_noise_floor()
        samples.append(nf)
        await asyncio.sleep(args.sample_interval)

    return {
        "freq_mhz": freq,
        "samples": len(samples),
        "noise_floor_avg": sum(samples) / len(samples),
        "noise_floor_min": min(samples),
        "noise_floor_max": max(samples),
        "noise_floor_stdev": statistics.pstdev(samples) if len(samples) > 1 else 0.0,
    }


# ----------------------------
# Graphing (after scan)
# ----------------------------

def png_name_from_out(out_path: str) -> str:
    p = Path(out_path)
    if p.suffix.lower() == ".csv":
        return str(p.with_suffix(".png"))
    return str(p) + ".png"


def generate_graph(csv_path: str, png_path: str, title: str):
    import matplotlib.pyplot as plt  # only needed at the end

    freqs = []
    avgs = []

    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                freqs.append(float(row["freq_mhz"]))
                avgs.append(float(row["noise_floor_avg"]))
            except (KeyError, ValueError, TypeError):
                continue

    if not freqs:
        print(f"[!] No data found in CSV to plot: {csv_path}")
        return

    plt.figure()
    plt.plot(freqs, avgs)
    plt.title(title)
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("Noise Floor (avg)")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(png_path, dpi=200)
    plt.close()


# ----------------------------
# CLI
# ----------------------------

async def main_async():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--usb", help="USB serial device (e.g. /dev/ttyUSB0)")
    g.add_argument("--tcp", help="TCP target HOST:PORT (e.g. 192.168.1.50:4242)")

    ap.add_argument("--debug", action="store_true", help="Print raw protocol frames (hex)")

    ap.add_argument("--start-mhz", type=float, default=915.0)
    ap.add_argument("--end-mhz", type=float, default=928.0)
    ap.add_argument("--step-mhz", type=float, default=0.125)

    ap.add_argument("--dwell-min", type=float, default=15)
    ap.add_argument("--sample-interval", type=float, default=5)

    ap.add_argument("--bw-khz", type=float, default=250)
    ap.add_argument("--sf", type=int, default=10)
    ap.add_argument("--cr", type=int, default=5)
    ap.add_argument("--settle-s", type=float, default=2)

    ap.add_argument("--out", default=None,
                    help="Output CSV filename (default auto-generated)")

    args = ap.parse_args()

    # Auto-generate filename if not provided
    if not args.out:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.out = f"meshcore-noisefloor-{int(args.bw_khz)}-{args.sf}-{args.cr}_{timestamp}.csv"

    # Precompute frequency list so we can show [i/x]
    freqs = list(frange(args.start_mhz, args.end_mhz, args.step_mhz))
    total = len(freqs)

    # ---- Startup output ----
    print(f"Output CSV : {args.out}")
    print(f"Freq range : {args.start_mhz} → {args.end_mhz} MHz (step {args.step_mhz} MHz) | total {total}")
    print(f"Radio      : BW {args.bw_khz} kHz | SF {args.sf} | CR {args.cr}")
    print("")

    # Select transport
    if args.usb:
        transport = USBTransport(args.usb)
    else:
        host, port = args.tcp.split(":")
        transport = TCPTransport(host, int(port))

    client = MeshCoreCompanionClient(transport, debug=args.debug)

    await client.connect()
    await client.handshake()

    fieldnames = [
        "freq_mhz",
        "samples",
        "noise_floor_avg",
        "noise_floor_min",
        "noise_floor_max",
        "noise_floor_stdev",
    ]

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames)
        writer.writeheader()

        for i, freq in enumerate(freqs, 1):
            print(f"[{i}/{total}] Measuring {freq} MHz")
            row = await measure_freq(client, freq, args)
            writer.writerow(row)
            f.flush()
            print(f"    avg={row['noise_floor_avg']:.2f}")

    await client.close()

    # ---- After scan: generate PNG ----
    png_path = png_name_from_out(args.out)
    freq_range_str = f"{args.start_mhz}-{args.end_mhz} MHz"
    title = (
        f"Meshcore Noise vs Frequency - BW: {int(args.bw_khz)} SF: {args.sf} CR: {args.cr} \n"
        f"Freq: {freq_range_str} Steps: {args.step_mhz}"
    )

    print("")
    print("Scan complete.")
    print(f"Generating graph from: {args.out}")
    print(f"Saving PNG to       : {png_path}")
    generate_graph(args.out, png_path, title)
    print("✅ Graph saved")


if __name__ == "__main__":
    asyncio.run(main_async())
