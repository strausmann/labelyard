<!--
SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# USB network bridge

Makes a USB-only P-touch (tested: PT-D600) look like a network printer to Labelyard: raw printing on
TCP 9100 and the printer's status over SNMP (UDP 161), from a small Linux computer the printer is
plugged into. Setup, settings and limits: <https://labelyard.app/install/printers/#usb-printers-through-a-bridge>.

| File | What it is |
|---|---|
| `ptouch_bridge.py` | The bridge, Python 3.10+, standard library only. Install as `/usr/local/bin/ptouch-bridge` |
| `ptouch-bridge.service` | The systemd unit: no root, settings as `PTB_*` environment variables |
| `test_ptouch_bridge.py` | Its tests: `python3 -m unittest discover -s examples/usb-bridge` |

## What the hub reads

| SNMP object | OID | From the printer's status |
|---|---|---|
| `hrPrinterStatus` | `1.3.6.1.2.1.25.3.5.1.1.1` | `3` idle, `4` printing (pages sent and not yet reported printed) |
| `hrPrinterDetectedErrorState` | `1.3.6.1.2.1.25.3.5.1.2.1` | no tape, cover open, cutter jam, other faults (RFC 3805 bits) |
| `prtInputMediaName` | `1.3.6.1.2.1.43.8.2.1.12.1.1` | loaded tape, for example `12mm(0.47")` |
| `prtMarkerLifeCount` | `1.3.6.1.2.1.43.10.2.1.4.1.1` | labels the printer reported as printed (`PTB_LABEL_COUNTER`) |
| Brother PJL id | `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` | `MFG:Brother;CMD:PT-CBP;MDL:<USB product name>;CLS:PRINTER;` |
| `prtGeneralSerialNumber`, `ifPhysAddress` | `…43.5.1.1.17.1`, `…2.2.1.6.1` | the printer's USB serial number, the bridge's network address |

The bridge answers a status question with the printer's reply to `ESC i S`; while a job prints it
does not ask the printer and uses the notifications the printer sends by itself. When the printer
is off or unplugged, it does not answer, so the hub shows the printer as offline.
