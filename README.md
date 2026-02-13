# 📡 MeshCore Noise Floor Scanner

Scan LoRa frequencies using the **MeshCore Companion protocol** and
automatically generate:

-   📄 CSV output (per-frequency noise statistics)
-   📊 PNG graph (Noise vs Frequency)
-   📈 Real-time progress display
-   🔌 Works over **USB Serial** or **TCP Companion**

------------------------------------------------------------------------

## 🚀 Features

-   Supports **USB** (`/dev/ttyUSB0`) and **TCP** (`HOST:PORT`)

-   Automatic Companion handshake

-   Per-frequency dwell sampling

-   Outputs:

    -   Average noise floor
    -   Min / Max
    -   Standard deviation

-   Auto-generated filename:

        meshcore-noisefloor-(bw)-(sf)-(cr)_(timestamp).csv

-   Automatically generates PNG graph when scan completes

-   Progress indicator:

        [1/105] Measuring 915.000 MHz

------------------------------------------------------------------------

## 📦 Requirements

-   Python 3.9+
-   `pyserial` (for USB mode)
-   `matplotlib` (for graph generation)

Install dependencies:

``` bash
pip install pyserial matplotlib
```

------------------------------------------------------------------------

## 🛠 Usage

### 🔵 TCP Mode (Recommended)

``` bash
python3 noisefloor.py   --tcp 10.1.1.114:5000   --bw-khz 125   --sf 11   --cr 8   --start-mhz 918.125   --end-mhz 918.300   --step-mhz 0.125   --dwell-min 0.1
```

### 🟢 USB Mode

``` bash
python3 noisefloor.py   --usb /dev/ttyUSB0   --bw-khz 125   --sf 11   --cr 8
```

------------------------------------------------------------------------

## ⚙️ Arguments

  Argument              Description
  --------------------- -----------------------------------------
  `--usb`               USB serial device (e.g. `/dev/ttyUSB0`)
  
  `--tcp`               TCP Companion endpoint (`HOST:PORT`)
  
  `--start-mhz`         Start frequency (default `915.0`)
  
  `--end-mhz`           End frequency (default `928.0`)
  
  `--step-mhz`          Frequency step size
  
  `--dwell-min`         Minutes to sample per frequency
  
  `--sample-interval`   Seconds between samples
  
  `--bw-khz`            Bandwidth (e.g. `125`, `250`)
  
  `--sf`                Spreading Factor
  
  `--cr`                Coding Rate
  
  `--settle-s`          Delay after changing frequency
  
  `--out`               Custom output filename
  
  `--debug`             Show raw Companion protocol frames

------------------------------------------------------------------------

## 📄 Output Files

### CSV

Columns:

-   `freq_mhz`
-   `samples`
-   `noise_floor_avg`
-   `noise_floor_min`
-   `noise_floor_max`
-   `noise_floor_stdev`

Example filename:

    meshcore-noisefloor-125-11-8_20260213-203047.csv

------------------------------------------------------------------------

### 📊 PNG Graph

Automatically generated at the end of the scan.

Example filename:

    meshcore-noisefloor-125-11-8_20260213-203047.png

Graph title format:

    Meshcore Noise vs Frequency - BW: (bw) SF: (sf) CR: (cr) Freq: (range) Steps: (step)

------------------------------------------------------------------------

## 🧠 How It Works

1.  Companion handshake:
    -   `CMD_DEVICE_QUERY`
    -   `CMD_APP_START`
2.  For each frequency:
    -   `CMD_SET_RADIO_PARAMS`
    -   `CMD_GET_STATS`
3.  Extract noise floor from `RESP_STATS`
4.  Save CSV row immediately
5.  Generate PNG graph at completion

------------------------------------------------------------------------

## ⚠️ Firmware Compatibility

Noise floor retrieval uses `CMD_GET_STATS`.

Tested working with:

-   MeshCore firmware `v1.12.x - Companion USB & Companion Wifi`

If you receive:

    GET_STATS failed (RESP_ERR code=1)

Your firmware likely does not expose radio stats over Companion on that
transport.

### Solution

-   Use `--tcp` if that endpoint supports stats
-   Or update firmware

------------------------------------------------------------------------

## 📜 License

MIT License
