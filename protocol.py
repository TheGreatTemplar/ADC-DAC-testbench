"""ASCII ADC stream and read-only protocol 0.0 with configurable CRC encoding."""

import re
from dataclasses import dataclass

ADC_CHANNEL_NAMES = ("Контроль целостности КЧЭ", "Обработка тревоги", "Контроль питания (батареи)")
ADC_CSV_COLUMNS = ("a0_kche_integrity_code", "a1_alarm_processing_code", "a2_battery_supply_code")
FW018_LENGTHS = {3: 1, 5: 2, 10: 1, 11: 5, 15: 1, 25: 2, 45: 0}


def describe_device_state(state):
    flags = [(1, "тревога"), (2, "неисправность"), (4, "обрыв кабеля"),
             (8, "отключен"), (32, "КЗ кабеля"), (128, "дистанционный контроль")]
    names = [name for mask, name in flags if state & mask]
    if state & ~0xAF:
        names.append(f"неизвестные биты 0x{state & ~0xAF:02X}")
    return ", ".join(names) or "норма"


def decode_working_snapshot(data):
    if len(data) != 5:
        raise ValueError("Working snapshot must contain 5 data bytes")
    return (int.from_bytes(data[:2], "little", signed=True),
            int.from_bytes(data[2:4], "little"), data[4])


class AdcStreamParser:
    """Test firmware sends ASCII A0/A1/A2 rows without a checksum."""

    row = re.compile(rb"A0: ([0-9A-Fa-f]{4}); A1: ([0-9A-Fa-f]{4}); A2: ([0-9A-Fa-f]{4});")

    def __init__(self):
        self.buffer = bytearray()
        self.rejected = 0

    def feed(self, chunk):
        self.buffer.extend(chunk)
        samples = []
        while b"\r" in self.buffer or b"\n" in self.buffer:
            end = next(index + 1 for index, byte in enumerate(self.buffer) if byte in (10, 13))
            line = bytes(self.buffer[:end - 1])
            del self.buffer[:end]
            # Firmware transmits CR only; also accept CR LF, including split reads.
            if not line:
                continue
            match = self.row.fullmatch(line)
            if match:
                samples.append(tuple(int(value, 16) for value in match.groups()))
            else:
                self.rejected += 1
        # Bound memory when baud/format is wrong and no line terminator arrives.
        if len(self.buffer) > 128:
            self.buffer.clear()
            self.rejected += 1
        return samples


def describe_adc_sample(sample):
    return "; ".join(f"A{index}: {value} код (0x{value:04X}, {name})"
                     for index, (value, name) in enumerate(zip(sample, ADC_CHANNEL_NAMES))) + "; без CRC"


def command_code(table_number, hex_numbers=False):
    return int(str(table_number), 16 if hex_numbers else 10)


def encode_request(command, checksum_mode="raw", data=b""):
    if not 0 <= command <= 255:
        raise ValueError("Command must fit in one byte")
    if checksum_mode not in ("raw", "hex"):
        raise ValueError("Unknown checksum encoding")
    value = (command + sum(data)) & 255
    checksum = bytes([value]) if checksum_mode == "raw" else f"{value:02X}".encode("ascii")
    return b"&" + f"{command:02X}".encode("ascii") + data.hex().upper().encode("ascii") + checksum + b"\r"


def command_description(command, hex_numbers=False, firmware="legacy"):
    if firmware == "fw018":
        names = {3: "запрос состояния", 5: "запрос версии ПО", 10: "порог (8 бит)",
                 11: "обработанный сигнал, порог и состояние", 15: "чтение ступени усиления",
                 25: "оценка фона", 45: "установка ступени усиления"}
        return f"Команда {command} (0x{command:02X}): " + names.get(command, "неизвестная команда")
    for number, name in [(3, "запрос состояния"), (5, "запрос версии ПО"),
                         (25, "запрос текущего уровня CH1")]:
        if command == command_code(number, hex_numbers):
            base = "HEX" if hex_numbers else "DEC"
            return f"Команда {number} ({base}, код 0x{command:02X}): {name}"
    return f"Неизвестная команда 0x{command:02X}"


def describe_request(packet, hex_numbers=False, firmware="legacy"):
    if len(packet) < 3 or packet[:1] != b"&" or any(byte not in b"0123456789abcdefABCDEF" for byte in packet[1:3]):
        return "Нераспознанный запрос"
    command = int(packet[1:3], 16)
    text = command_description(command, hex_numbers, firmware)
    if firmware == "fw018" and command == 45:
        try:
            data = bytes.fromhex(packet[3:5].decode("ascii"))
            if len(data) == 1 and packet == encode_request(command, "hex", data):
                return text + f"; ступень {data[0]}; CRC hex: OK"
        except ValueError:
            pass
    for mode in ("raw", "hex"):
        if packet == encode_request(command, mode):
            return text + f"; CRC {mode}: OK"
    return text + "; формат или CRC запроса не подтверждены"


def describe_reply(reply, hex_numbers=False, firmware="legacy"):
    text = command_description(reply.command, hex_numbers, firmware)
    if reply.device_error:
        text += "; ОШИБКА: прибор отклонил команду"
        if reply.data:
            text += f", код {reply.data[0]}"
    elif firmware == "fw018" and reply.command == 11:
        signal, trigger, state = decode_working_snapshot(reply.data)
        text += f"; сигнал {signal}; порог ±{trigger}; состояние 0x{state:02X} — {describe_device_state(state)}"
    elif firmware == "fw018" and reply.command in (10, 25):
        text += f"; ответ: {int.from_bytes(reply.data, 'little')} ед. алгоритма"
    elif firmware == "fw018" and reply.command == 15:
        text += f"; ступень {reply.data[0]}"
    elif firmware == "fw018" and reply.command == 45:
        text += "; запись принята; требуется контрольное чтение"
    elif reply.command == command_code(5, hex_numbers):
        text += f"; ответ: версия {reply.data[0]}.{reply.data[1]}"
    elif reply.command == command_code(25, hex_numbers):
        value = int.from_bytes(reply.data, "little")
        text += f"; ответ: уровень {value} код = {value * 0.6105:.3f} мВ"
    elif reply.command == command_code(3, hex_numbers):
        state = reply.data[0]
        text += f"; ответ: состояние 0x{state:02X} — {describe_device_state(state)}"
    return text + f"; CRC {reply.checksum_mode}: OK"


@dataclass
class Reply:
    command: int
    data: bytes
    device_error: bool
    checksum_mode: str
    raw: bytes


class ReplyParser:
    """Frames have known data lengths. A raw CRC may itself be CR, ! or ?."""

    def __init__(self, lengths, error_size=0, checksum_modes=("raw", "hex")):
        self.lengths = lengths
        self.error_size = error_size
        self.checksum_modes = checksum_modes
        self.buffer = bytearray()
        self.rejected = 0

    def feed(self, chunk):
        self.buffer.extend(chunk)
        replies = []
        while self.buffer:
            if self.buffer[0] not in (ord("!"), ord("?")):
                del self.buffer[0]
                continue
            if len(self.buffer) < 3:
                break
            try:
                header = bytes(self.buffer[1:3])
                if any(byte not in b"0123456789abcdefABCDEF" for byte in header):
                    raise ValueError("Invalid command")
                command = int(header, 16)
            except ValueError:
                del self.buffer[0]
                self.rejected += 1
                continue
            device_error = self.buffer[0] == ord("?")
            if command not in self.lengths:
                del self.buffer[0]
                self.rejected += 1
                continue
            size = self.error_size if device_error else self.lengths[command]
            end = 3 + size * 2
            if len(self.buffer) < end + 2:
                break
            try:
                body = bytes(self.buffer[3:end])
                if any(byte not in b"0123456789abcdefABCDEF" for byte in body):
                    raise ValueError("Invalid data")
                data = bytes.fromhex(body.decode("ascii"))
            except ValueError:
                del self.buffer[0]
                self.rejected += 1
                continue
            checksum = (command + sum(data)) & 255
            mode = None
            length = 0
            if "raw" in self.checksum_modes and self.buffer[end] == checksum and self.buffer[end + 1] == 13:
                mode, length = "raw", end + 2
            elif len(self.buffer) >= end + 3:
                crc_text = bytes(self.buffer[end:end + 2])
                if ("hex" in self.checksum_modes and all(byte in b"0123456789abcdefABCDEF" for byte in crc_text)
                        and int(crc_text, 16) == checksum and self.buffer[end + 2] == 13):
                    mode, length = "hex", end + 3
            else:
                break
            if mode is None:
                del self.buffer[0]
                self.rejected += 1
                continue
            raw = bytes(self.buffer[:length])
            del self.buffer[:length]
            replies.append(Reply(command, data, device_error, mode, raw))
        return replies
