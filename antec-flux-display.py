#!/usr/bin/env python3
"""
Antec Flux Pro Display Service

Reads CPU and GPU temperatures from Linux hwmon and sends them to the
Antec Flux Pro front-panel display over USB.

Requires:
    python3-pyusb
    libusb1

USB device:
    Vendor ID:  2022
    Product ID: 0522
"""

import glob
import subprocess
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import usb.core
import usb.util


VENDOR_ID = 0x2022
PRODUCT_ID = 0x0522
INTERFACE_NUMBER = 0
ENDPOINT_OUT = 0x03

UPDATE_INTERVAL_SECONDS = 1.0
USB_WRITE_TIMEOUT_MS = 1_000
RECONNECT_INTERVAL_SECONDS = 2.0
NVIDIA_SMI = "/usr/bin/nvidia-smi"


def find_hwmon_paths(device_name: str) -> list[Path]:
    """Return all hwmon paths matching the requested device name."""
    matching_paths: list[Path] = []

    for hwmon_path_string in glob.glob("/sys/class/hwmon/hwmon*"):
        hwmon_path = Path(hwmon_path_string)

        try:
            current_device_name = (hwmon_path / "name").read_text().strip()
        except (OSError, UnicodeError):
            continue

        if current_device_name == device_name:
            matching_paths.append(hwmon_path)

    return matching_paths


def find_hwmon_path(device_name: str) -> Optional[Path]:
    """Return the first hwmon path matching the requested device name."""
    matching_paths = find_hwmon_paths(device_name)
    return matching_paths[0] if matching_paths else None


def read_temperature(
    hwmon_path: Optional[Path],
    input_file: str = "temp1_input",
) -> Optional[float]:
    """Read a temperature from hwmon and return degrees Celsius."""
    if hwmon_path is None:
        return None

    try:
        raw_value = (hwmon_path / input_file).read_text().strip()
        temperature = int(raw_value) / 1000.0
    except (OSError, UnicodeError, ValueError):
        return None

    if temperature <= 0:
        return None

    return temperature


def read_nvidia_smi_temperature() -> Optional[float]:
    """Read the first NVIDIA GPU temperature from nvidia-smi."""
    try:
        result = subprocess.run(
            [
                NVIDIA_SMI,
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    output_lines = result.stdout.splitlines()
    if not output_lines:
        return None

    first_line = output_lines[0].strip()

    try:
        temperature = float(first_line)
    except ValueError:
        return None

    if temperature <= 0:
        return None

    return temperature


def find_temperature_input_file(
    hwmon_path: Optional[Path],
    preferred_label_terms: tuple[str, ...] = (),
) -> str:
    """Return the best temp*_input file for a hwmon device."""
    fallback_input_file = "temp1_input"

    if hwmon_path is None:
        return fallback_input_file

    input_files = sorted(hwmon_path.glob("temp*_input"))
    if not input_files:
        return fallback_input_file

    for input_path in input_files:
        label_path = input_path.with_name(
            input_path.name.replace("_input", "_label")
        )

        try:
            label = label_path.read_text().strip().lower()
        except (OSError, UnicodeError):
            continue

        if any(term in label for term in preferred_label_terms):
            return input_path.name

    if (hwmon_path / fallback_input_file).exists():
        return fallback_input_file

    return input_files[0].name


def encode_temperature(temperature: Optional[float]) -> list[int]:
    """
    Encode a temperature as [tens, ones, tenths].

    Missing or invalid temperatures are encoded as 0xEE bytes, which makes
    the display show --.-.
    """
    if temperature is None:
        return [0xEE, 0xEE, 0xEE]

    clamped_temperature = min(max(temperature, 0.0), 99.9)
    temperature_tenths = round(clamped_temperature * 10)

    whole_degrees = temperature_tenths // 10
    tenths = temperature_tenths % 10

    tens = whole_degrees // 10
    ones = whole_degrees % 10

    return [tens, ones, tenths]


def build_packet(
    cpu_temperature: Optional[float],
    gpu_temperature: Optional[float],
) -> bytes:
    """
    Build the 12-byte display packet.

    Format:
        55 AA 01 01 06
        CPU tens, ones, tenths
        GPU tens, ones, tenths
        checksum
    """
    payload = [0x55, 0xAA, 0x01, 0x01, 0x06]
    payload.extend(encode_temperature(cpu_temperature))
    payload.extend(encode_temperature(gpu_temperature))
    payload.append(sum(payload) & 0xFF)

    return bytes(payload)


def close_display(device: Optional[usb.core.Device]) -> None:
    """Release and dispose of a previously opened USB device."""
    if device is None:
        return

    try:
        usb.util.release_interface(device, INTERFACE_NUMBER)
    except usb.core.USBError:
        pass

    try:
        usb.util.dispose_resources(device)
    except usb.core.USBError:
        pass


def open_display() -> Optional[usb.core.Device]:
    """Open and claim the Antec display USB interface."""
    device = usb.core.find(
        idVendor=VENDOR_ID,
        idProduct=PRODUCT_ID,
    )

    if device is None:
        return None

    try:
        if device.is_kernel_driver_active(INTERFACE_NUMBER):
            device.detach_kernel_driver(INTERFACE_NUMBER)
    except (usb.core.USBError, NotImplementedError):
        pass

    try:
        device.set_configuration()
    except usb.core.USBError as error:
        # EBUSY usually means the device is already configured.
        if getattr(error, "errno", None) not in (None, 16):
            close_display(device)
            raise

    try:
        usb.util.claim_interface(device, INTERFACE_NUMBER)
    except usb.core.USBError:
        close_display(device)
        raise

    return device


def format_temperature(temperature: Optional[float]) -> str:
    """Format a temperature for logging."""
    return "--.-" if temperature is None else f"{temperature:.1f}"


def main() -> int:
    cpu_hwmon_path = find_hwmon_path("k10temp")
    if cpu_hwmon_path is None:
        print(
            "ERROR: k10temp hwmon device not found. "
            "Check whether the k10temp module is loaded.",
            file=sys.stderr,
        )
        return 1

    gpu_hwmon_path = find_hwmon_path("nvidia")
    gpu_temperature_input_file = find_temperature_input_file(
        gpu_hwmon_path,
        ("gpu", "core", "edge"),
    )
    fallback_gpu_hwmon_path = None
    fallback_gpu_temperature_input_file = "temp1_input"

    if gpu_hwmon_path is None:
        fallback_gpu_hwmon_path = find_hwmon_path("amdgpu")
        fallback_gpu_temperature_input_file = find_temperature_input_file(
            fallback_gpu_hwmon_path,
            ("gpu", "core", "edge"),
        )

    print(f"CPU sensor: {cpu_hwmon_path}")

    if gpu_hwmon_path is None:
        print(
            "WARNING: nvidia hwmon device not found. "
            "Will try nvidia-smi before falling back.",
            file=sys.stderr,
        )

        if fallback_gpu_hwmon_path is None:
            print(
                "WARNING: amdgpu fallback hwmon device not found. "
                "GPU temperature will show --.- if nvidia-smi also fails.",
                file=sys.stderr,
            )
        else:
            print(
                "Fallback GPU sensor: "
                f"{fallback_gpu_hwmon_path}/"
                f"{fallback_gpu_temperature_input_file}",
                file=sys.stderr,
            )
    else:
        print(f"GPU sensor: {gpu_hwmon_path}/{gpu_temperature_input_file}")

    running = True

    def handle_shutdown_signal(signum: int, _frame: object) -> None:
        nonlocal running
        print(f"\nReceived signal {signum}; stopping.")
        running = False

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    device: Optional[usb.core.Device] = None

    try:
        while running:
            if device is None:
                try:
                    device = open_display()
                except usb.core.USBError as error:
                    print(
                        f"USB connection error: {error}",
                        file=sys.stderr,
                    )

                if device is None:
                    print(
                        "Display 2022:0522 not available; retrying.",
                        file=sys.stderr,
                    )
                    time.sleep(RECONNECT_INTERVAL_SECONDS)
                    continue

                print(
                    f"Display connected: "
                    f"{VENDOR_ID:04x}:{PRODUCT_ID:04x}"
                )

            cpu_temperature = read_temperature(
                cpu_hwmon_path,
                "temp1_input",
            )
            gpu_temperature = read_temperature(
                gpu_hwmon_path,
                gpu_temperature_input_file,
            )
            if gpu_temperature is None:
                gpu_temperature = read_nvidia_smi_temperature()
            if gpu_temperature is None:
                gpu_temperature = read_temperature(
                    fallback_gpu_hwmon_path,
                    fallback_gpu_temperature_input_file,
                )

            packet = build_packet(
                cpu_temperature,
                gpu_temperature,
            )

            try:
                bytes_written = device.write(
                    ENDPOINT_OUT,
                    packet,
                    timeout=USB_WRITE_TIMEOUT_MS,
                )

                if bytes_written != len(packet):
                    raise usb.core.USBError(
                        f"Short USB write: {bytes_written}/{len(packet)} bytes"
                    )
            except usb.core.USBError as error:
                print(
                    f"USB write error: {error}; reconnecting.",
                    file=sys.stderr,
                )
                close_display(device)
                device = None
                time.sleep(RECONNECT_INTERVAL_SECONDS)
                continue

            time.sleep(UPDATE_INTERVAL_SECONDS)

    finally:
        close_display(device)
        print("Display service stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
