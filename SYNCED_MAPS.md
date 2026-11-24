# UMR hardware telemetry and maps

## 1. Get the branch on Windows

Close your running dashboard with Ctrl+C. In PowerShell inside your existing
`umracingteam` folder, run the following while you have internet.

```powershell
git status --short
git fetch origin
git switch feature/synced-telemetry-maps
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If Git reports a conflict with local edits, save those edits before switching.
For a new virtual environment, first run `py -3.13 -m venv .venv`.

## 2. Prepare the boards

Start with your current ADS1115 and ESP32 wiring. The firmware reads brake on A0
and throttle on A1. It alternates them every 10 ms and targets 50 Hz per pedal.
Each result carries the STM32 time of the estimated ADC conversion midpoint.

With the boards powered off, check the labels and logic levels on your actual
GPS breakout. The following connections assume compatible 3.3 V logic and a
free PB0 on the STM32. Keep the existing common ground.

| Connection | Purpose |
|---|---|
| GPS TX → STM32 PA3 | Existing GPS data path, initially 9,600 baud |
| STM32 PA2 → GPS RX | Lets the sketch request 5 Hz and RMC output |
| GPS PPS or TIMEPULSE → STM32 PB0 | Rising-edge 1 pulse per second aligned to UTC for GPS/pedal matching |

If the breakout has no accessible PPS output, the pedal graphs and GPS map
still work. The dashboard displays **Waiting for PPS** and withholds aligned
pedal colours. An onboard blinking LED alone does not establish a connection to PB0.

Open `firmware/STM32_wireless/STM32_wireless.ino` from this branch in Arduino
IDE. Select **Generic STM32F4 series → Generic F407VGTx**, **STM32CubeProgrammer
(SWD)**, then upload by ST-LINK. Your existing ESP32 TCP bridge forwards the
new frames. Reuse the existing ESP32 sketch and current throttle calibration.

The sketch requests volatile u-blox M8 settings shortly after STM32 boot. Power
the receiver before or with the STM32. If the receiver starts later, reset the
STM32 once the receiver is ready. It selects RMC and a 200 ms navigation period
on the current UART. If you previously set the receiver to 38,400 baud, set
`GPS_BAUD` in the sketch to 38400 before uploading. For a separately configured
receiver, use `CONFIGURE_GPS_5HZ = false`. The measured rate on the dashboard
confirms the result. Rate changes also affect other devices sharing that receiver output.

## 3. Run a stationary outdoor bench check

Connect the laptop to **UMR-GPS-Test**. From the repo folder, run:

```powershell
$log = "logs/bench-$(Get-Date -Format yyyyMMdd-HHmmss).log"
.\.venv\Scripts\python.exe dashboard.py --hardware-port socket://192.168.4.1:9000 --hardware-record $log
```

Open [the dashboard](http://127.0.0.1:8050). Use the existing
`--throttle-calibration-mv RELEASED PRESSED --save-throttle-calibration` option
with your measured millivolt endpoints if the throttle still reads in mV. For
brake percentage, use your measured `--brake-calibration-v RELEASED FULL_SCALE`
endpoints. The defaults of 0.490 and 4.500 V are a provisional voltage span.

1. Give the antenna clear sky and wait for a valid fix. Operate each pedal on
   the bench and check both traces span their calibrated range.
2. Read the rates beneath the map. Targets are about **50 Hz throttle, 50 Hz
   brake, and 5 Hz GPS**. These are rolling averages of acquisition intervals.
   If GPS stays near 1 Hz, check PA2 → GPS RX, receiver baud and UBX input
   support. With zero valid fixes, check the GPS data path and antenna first.
3. Look for **PPS aligned** after valid fixes and regular pulses. The max pedal
   offset is the larger time difference between this fix and its matched pedal
   samples. The match limit is **15 ms per pedal**. It measures sample spacing
   on the shared clock. Receiver position error and physical sensor response
   are separate. If one input is missing, expired, or uncalibrated, its value
   stays unavailable. Hover the car for both percentages or a trace point to
   see its time cursor within the rolling pedal history.
4. If the screen says **Waiting for PPS**, check that the receiver provides
   a 1 Hz rising UTC pulse and that PB0 is available. The matcher pairs the RMC
   fractional UTC second with that pulse. It assumes the RMC sentence starts
   within 350 ms of its epoch. Keep the dedicated UART lightly loaded. A whole
   second of receiver-side queueing is ambiguous with PPS plus fractional time.
5. Switch through the three map backgrounds. In offline mode, disconnect the
   internet while keeping the ESP32 link, then pan and zoom. With stationary
   hardware the marker may wander with GPS accuracy. GPS loss should freeze
   the last position. Raw telemetry is written to the specified file. Stop
   with Ctrl+C to close the recording.

## 4. Use the venue map and record the exact track

| Mode | Display |
|---|---|
| Live Street · Online | Street tiles around the car wherever the laptop has internet. A second network path such as USB tethering can supply internet while Wi-Fi carries telemetry. |
| Live Street · Offline | Local street geometry around 3.3701754° N, 101.5740753° E. Hover street markers for names. Outside the saved coverage, GPS and the trail continue. |
| Track Only | A geographic trail until a measured course is loaded. Expand the supplied competition drawing for reference. After loading a course the view stays centred on it. |

The saved street extract is approximately 1.7 km across and 76 kB. The PNG is
a reference image. The venue pin alone cannot align its geometry.

During an authorised course walk, bring the powered GPS unit and recording
laptop within radio range. Acquire a valid fix at Start, start a **fresh**
recording, walk the actual centreline to Finish, and stop the recording there.
Use a dedicated file name, for example `--hardware-record logs/course-walk.log`.
Convert the walk and run the dashboard with the new outline:

```powershell
.\.venv\Scripts\python.exe course_from_log.py logs/course-walk.log tracks/competition-course.geojson
.\.venv\Scripts\python.exe dashboard.py --hardware-port socket://192.168.4.1:9000 --course tracks/competition-course.geojson
```

The converter checks for invalid fixes, time gaps and distinct positions. It
accepts both timestamped and legacy RMC frames. It creates a new output file.
The outline is visual. Lap timing uses the separate finish gate and checkpoint
configuration described in the README.

## Map data and receiver references

Road geometry and names in `assets/venue-roads.geojson` are © OpenStreetMap
contributors, available under the Open Database License. The extract comes
from the OSM map API for the bounding box recorded inside the GeoJSON.
[Source and licence](https://www.openstreetmap.org/copyright)

The receiver uses UBX-CFG-MSG and UBX-CFG-RATE on the current UART.
[u-blox M8 protocol manual](https://content.u-blox.com/sites/default/files/products/documents/u-blox8-M8_ReceiverDescrProtSpec_UBX-13003221.pdf)

## Two ESP32 boards

The car ESP32 hosts **UMR-GPS-Test** and receives the STM32's telemetry UART.
The pit ESP32 joins that network and forwards the same timestamped frames via
USB. The pit laptop can stay on its normal Wi-Fi for online street maps.

### Metatronik ESP32-S3 on the car

1. Open `firmware/telemetry_wifi_esp32/telemetry_wifi_esp32.ino` in Arduino IDE.
   For the generic ESP32-S3 N16R8, select **ESP32S3 Dev Module** in the Espressif
   ESP32 package and the board's **UART** USB port. Use a USB data cable.
2. Upload over USB with the board separate from car power. If your exact board
   uses an external antenna, confirm its antenna/connector arrangement with the
   board documentation and attach the compatible antenna before powering it
   for a radio test.
3. With power off, move the car's **STM32 PB10 (TX) → ESP32 GPIO18 (RX)** and
   **GND → GND** connections to the Metatronik board. Use the board's documented
   power input and your existing compatible 3.3 V UART wiring. The car ESP32
   requires only RX for telemetry. Power the car telemetry and check that
   **UMR-GPS-Test** appears.

### Original ESP32 at the pit

1. Open `firmware/telemetry_pit_esp32/telemetry_pit_esp32.ino` and upload it to
   the original ESP32. For the existing ESP32-S3 with a USB-UART bridge, use
   **ESP32S3 Dev Module**, the **UART** USB-C port and **USB CDC On Boot →
   Disabled**. If using the S3's **native USB** port instead, use **USB CDC On
   Boot → Enabled** and select the COM port that appears after reset.
2. Keep the pit board connected to the laptop by USB. It needs no connection
   to the STM32 or the car's ground. Open Arduino Serial Monitor at **115200**
   to check for raw lines such as `STM32_ALIVE`, `UMR_ADC` and `UMR_RMC`.
   A raw `UMR_RMC` line can appear before GPS acquires a fix.
3. Close Serial Monitor and stop any dashboard still reading the car's Wi-Fi
   socket. The car sketch serves one TCP client. From PowerShell, start the
   dashboard with the pit board's COM port, replacing `COM7` in this example.

```powershell
.\.venv\Scripts\python.exe dashboard.py --hardware-port COM7
```

For the first run on the new port, if the throttle shows mV, reuse your
previous measured released and pressed endpoints with
`--throttle-calibration-mv RELEASED PRESSED --save-throttle-calibration`. The
dashboard stores throttle calibration per port. Run the same COM command with
your existing `--brake-calibration-v` values if you calibrated the brake.

Open [the dashboard](http://127.0.0.1:8050) and check both pedal graphs and the
GPS status. With the updated STM32 sketch and a valid fix, look for about 50 Hz
per pedal, about 5 Hz GPS and **PPS aligned** when PPS and both calibrations are
available. Verify the link first with both boards nearby, then at the intended
pit location.
