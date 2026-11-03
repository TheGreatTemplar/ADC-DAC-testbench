"""Read live algorithm thresholds for the verified testbench_1 firmware via SWD."""

import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from qt_compat import QtCore


# Check the acquisition loop and threshold calculation before interpreting RAM.
FIRMWARE_BLOCKS = (
    (0x080028B0, 0xB0, "b53f01bbc6fcc3384e659a48ebdc5993ba15721a2b8652acf8354ee4f1b8a5a3"),
    (0x08002BC4, 0x158, "2da8ef6ed888172e9436a3d7b0741c50952cdce43e2aee436cca5e0ffd9c90d0"),
)
RAM_ADDRESS = 0x2000033C
FW018_BLOCKS = (
    (0x08002F54, 0x440, "c4b5eb6f021aff24c03357b9490094b035cd5e555814c1fe7cfff67f9ac0ab76"),
    (0x080035C8, 0x15C, "1e6731ef6888dfaf4addb7d9de6aab5a73a92db255e4a7e62f23ab3fdf26906e"),
)


@dataclass
class ThresholdSnapshot:
    signal: float
    noise: Optional[float]
    trigger: float
    release: Optional[float]
    source: str = "SWD"


def find_stlink_cli():
    command = shutil.which("ST-LINK_CLI.exe")
    if command:
        return command
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(variable)
        if root:
            path = Path(root) / "STMicroelectronics/STM32 ST-LINK Utility/ST-LINK Utility/ST-LINK_CLI.exe"
            if path.is_file():
                return str(path)
    raise RuntimeError("Не найдена ST-LINK_CLI.exe. Установите STM32 ST-LINK Utility и драйвер ST-Link.")


def read_thresholds(cli, firmware="adc_stream"):
    blocks = FW018_BLOCKS if firmware == "fw018" else FIRMWARE_BLOCKS
    ram_address = 0x2000044C if firmware == "fw018" else RAM_ADDRESS
    args = [cli, "-c", "SWD", "HOTPLUG"]
    for address, size, _ in blocks:
        args.extend(["-r32", f"0x{address:08X}", f"0x{size:X}"])
    # CLI 3.6 interprets the count as bytes, even for -r32.
    args.extend(["-r32", f"0x{ram_address:08X}", "0x18"])
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = result.stdout.decode("ascii", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("Нет доступа к ST-Link. Закройте ST-LINK Utility и проверьте подключение SWD.")
    memory = {}
    for line in output.splitlines():
        match = re.fullmatch(r"\s*0x([0-9A-Fa-f]{8})\s*:\s*((?:[0-9A-Fa-f]{8}\s*)+)\s*", line)
        if match:
            address = int(match[1], 16)
            data = b"".join(struct.pack("<I", int(word, 16)) for word in match[2].split())
            memory.update((address + index, value) for index, value in enumerate(data))

    def block(address, size):
        try:
            return bytes(memory[address + index] for index in range(size))
        except KeyError:
            raise RuntimeError("ST-Link не вернул полный блок памяти. Значения порогов недоступны.") from None

    for address, size, expected in blocks:
        if hashlib.sha256(block(address, size)).hexdigest() != expected:
            raise RuntimeError("Прошивка отличается от выбранного профиля. Адреса порогов не подтверждены.")
    signal, _, _, noise, trigger, release = struct.unpack("<6f", block(ram_address, 0x18))
    if (not all(math.isfinite(value) for value in (signal, noise, trigger, release))
            or not 0 <= release <= trigger <= 2048 or noise < 0):
        raise RuntimeError("Некорректные значения порогов в ОЗУ; чтение остановлено.")
    return ThresholdSnapshot(signal, noise, trigger, release)


class ThresholdWorker(QtCore.QThread):
    updated = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, parent=None, firmware="adc_stream"):
        super().__init__(parent)
        self.stop_event = threading.Event()
        self.firmware = firmware

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                cli = find_stlink_cli()
                snapshot = read_thresholds(cli, self.firmware)
                if not self.stop_event.is_set():
                    self.updated.emit(snapshot)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                if not self.stop_event.is_set():
                    self.failed.emit(str(error))
            self.stop_event.wait(1)
