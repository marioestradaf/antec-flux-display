# Antec Flux Pro Display Service for Fedora

A lightweight Python systemd service that sends CPU and GPU temperatures to the
Antec Flux Pro case display on Fedora Linux. It talks directly to the display's
internal USB device, so the Windows-only Antec iUnity software is not required.

This project is a Fedora-focused local recreation inspired by Nish Tahir's
original Linux work:

- [Building an Ubuntu service for my Antec Flux Pro](https://nishtahir.com/building-an-ubuntu-service-for-my-antec-flux-display/)
- [nishtahir/antec-flux-pro-display](https://github.com/nishtahir/antec-flux-pro-display)

Credit to Nish Tahir for documenting the reverse engineering process and the
USB packet format. This repository keeps the same hardware idea but implements a
small Python service and Fedora installer instead of the original Rust/Debian
package.

## What It Does

- Reads CPU temperature from the Linux `hwmon` sensor named `k10temp`
- Reads NVIDIA GPU temperature from `hwmon`, then falls back to `nvidia-smi`
- Falls back to an `amdgpu` `hwmon` sensor when no NVIDIA source is available
- Sends a temperature packet to USB device `2022:0522` once per second
- Installs as a restricted systemd service user named `antec-flux`
- Installs a udev rule so the service can access the display without running as
  root

** The installer is intentionally scoped to Fedora 44. **

## How It Works

The Antec Flux Pro has a small display on the case panel that shows CPU and GPU
temperatures. The display is connected through an internal USB 2.0 header. On
Windows, Antec iUnity reads system temperatures and writes them to this USB
device. On Linux, this service does the same thing using `pyusb` and kernel
sensor data.

The display uses:

- Vendor ID: `2022`
- Product ID: `0522`
- Interrupt OUT endpoint: `0x03`

The service sends a 12-byte payload:

| Byte | Value | Description |
| ---- | ----- | ----------- |
| 0 | `0x55` | Header |
| 1 | `0xAA` | Header |
| 2 | `0x01` | Command |
| 3 | `0x01` | Command |
| 4 | `0x06` | Command |
| 5 | `0x00`-`0x09` | CPU temperature tens digit |
| 6 | `0x00`-`0x09` | CPU temperature ones digit |
| 7 | `0x00`-`0x09` | CPU temperature tenths digit |
| 8 | `0x00`-`0x09` | GPU temperature tens digit |
| 9 | `0x00`-`0x09` | GPU temperature ones digit |
| 10 | `0x00`-`0x09` | GPU temperature tenths digit |
| 11 | checksum | Sum of bytes 0-10, modulo 256 |

Missing temperatures are encoded as `0xEE 0xEE 0xEE`, which makes the display
show `--.-` for that line.

## Files

- `install.sh`: Fedora 44 installer for dependencies, service account, udev,
  and systemd
- `antec-flux-display.py`: Python service that reads temperatures and updates
  the USB display
- `70-antec-flux-display.rules`: reference udev rule for device `2022:0522`
- `antec-flux-display.service`: reference systemd unit; the installer generates
  and installs a hardened unit automatically

## Requirements

- Fedora 44
- Python 3
- Antec Flux Pro display cable connected to an internal USB header
- AMD CPU sensor exposed as `k10temp`
- NVIDIA GPU temperature exposed through `nvidia` `hwmon` or `nvidia-smi`
- Optional AMD GPU or iGPU fallback sensor exposed as `amdgpu`
- Root access for installation

The installer installs these Fedora packages:

```bash
python3
python3-pyusb
libusb1
systemd-udev
```

## Install

From this repository directory, run:

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer will:

1. verify that the system is Fedora 44
2. validate the Python service syntax
3. install required Fedora packages
4. create the `antec-flux` system user and group
5. add `antec-flux` to `video` and `render` when those groups exist
6. install the Python service to `/usr/local/libexec/antec-flux-display/`
7. install the udev rule to `/etc/udev/rules.d/70-antec-flux-display.rules`
8. install, enable, and start `antec-flux-display.service`

## Service Commands

Check service status:

```bash
systemctl status antec-flux-display.service
```

View live logs:

```bash
journalctl -u antec-flux-display.service -f
```

Restart the service:

```bash
sudo systemctl restart antec-flux-display.service
```

Stop the service:

```bash
sudo systemctl stop antec-flux-display.service
```

Enable automatic start at boot:

```bash
sudo systemctl enable antec-flux-display.service
```

Disable automatic start at boot:

```bash
sudo systemctl disable antec-flux-display.service
```

## Configuration

There is no separate config file yet. Sensor selection lives in
`/usr/local/libexec/antec-flux-display/antec-flux-display.py` after
installation, or in `antec-flux-display.py` before reinstalling.

The default CPU sensor is:

```python
cpu_hwmon_path = find_hwmon_path("k10temp")
```

The default GPU order is:

1. `nvidia` `hwmon`
2. `nvidia-smi`
3. `amdgpu` `hwmon`

For AMD-only systems, change the primary GPU lookup to:

```python
gpu_hwmon_path = find_hwmon_path("amdgpu")
```

For Intel CPUs, change the CPU lookup to:

```python
cpu_hwmon_path = find_hwmon_path("coretemp")
```

The service chooses the best available `temp*_input` file by looking for labels
such as `gpu`, `core`, or `edge`, then falls back to `temp1_input`.

Common sensor files:

| Sensor | File | Meaning |
| ------ | ---- | ------- |
| `k10temp` | `temp1_input` | AMD Tctl |
| `k10temp` | `temp3_input` | AMD CCD temperature, when exposed |
| `amdgpu` | `temp1_input` | GPU edge |
| `amdgpu` | `temp2_input` | GPU junction or hotspot |
| `amdgpu` | `temp3_input` | GPU memory, when exposed |

## Security Check

The installed systemd service runs as the restricted `antec-flux` user, not as
root. It also enables systemd hardening options such as `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome=yes`, and an empty capability set.

To inspect the service hardening score:

```bash
systemd-analyze security antec-flux-display.service
```

## Troubleshooting

If the display shows `--.-`, first check whether the service is running:

```bash
systemctl status antec-flux-display.service
journalctl -u antec-flux-display.service -f
```

If the CPU sensor is missing, check the available `hwmon` sensors:

```bash
cat /sys/class/hwmon/hwmon*/name
```

If the USB display is not detected, check for the expected USB device:

```bash
lsusb | grep -i '2022:0522'
```

If the NVIDIA GPU temperature does not appear, inspect the available NVIDIA
`hwmon` temperature inputs and labels:

```bash
for hwmon in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$hwmon/name" 2>/dev/null)" = "nvidia" ] || continue
    echo "$hwmon"
    grep -H . "$hwmon"/temp*_label "$hwmon"/temp*_input 2>/dev/null
done
```

If no `nvidia` `hwmon` device appears, check whether `nvidia-smi` can read the
GPU temperature:

```bash
nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits
```

After installation, check whether the service user can read it too:

```bash
sudo runuser -u antec-flux -- /usr/bin/nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits
```

Reload udev rules manually if needed:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=2022 --attr-match=idProduct=0522 --action=change
```

Some motherboard USB headers may be slow to enumerate this device during boot.
Trying a different internal USB 2.0 header can help. If needed, the Linux USB
quirk `usbcore.quirks=2022:0522:gk` may also avoid boot delays on affected
systems.

## Uninstall

Stop and disable the service:

```bash
sudo systemctl disable --now antec-flux-display.service
```

Remove installed service files:

```bash
sudo rm -f /etc/systemd/system/antec-flux-display.service
sudo rm -f /etc/udev/rules.d/70-antec-flux-display.rules
sudo rm -rf /usr/local/libexec/antec-flux-display
```

Reload systemd and udev:

```bash
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
```

Optionally remove the service account:

```bash
sudo userdel antec-flux
sudo groupdel antec-flux
```

## Hardware Compatibility

Confirmed target:

- Antec Flux Pro
- Tested with: AMD Ryzen 7 9800X3D + NVIDIA RTX 5080

Likely compatible:

- Other Antec cases that use the same iUnity display module and USB device
  `2022:0522`

## Acknowledgements

This work was inspired by Nish Tahir's
[Antec Flux Pro display project](https://github.com/nishtahir/antec-flux-pro-display)
and his write-up,
[Building an Ubuntu service for my Antec Flux Pro](https://nishtahir.com/building-an-ubuntu-service-for-my-antec-flux-display/).
His post documents the iUnity reverse engineering process, identifies the USB
device IDs, and explains the temperature packet structure used here.

Nish's repository also credits earlier community work by AKoskovich. Thanks to
the people who shared the first Linux notes for this display.

## License

License

This project is licensed under the MIT License. See the LICENSE file for details.
