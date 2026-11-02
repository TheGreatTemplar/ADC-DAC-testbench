"""Read-only subset of protocol 0.0; checksum assumptions remain configurable."""

from dataclasses import dataclass


def command_code(table_number, hex_numbers=False):
    return int(str(table_number), 16 if hex_numbers else 10)


def encode_request(command, checksum_mode="raw"):
    if not 0 <= command <= 255:
        raise ValueError("Command must fit in one byte")
    if checksum_mode not in ("raw", "hex"):
        raise ValueError("Unknown checksum encoding")
    checksum = bytes([command]) if checksum_mode == "raw" else f"{command:02X}".encode("ascii")
    return b"&" + f"{command:02X}".encode("ascii") + checksum + b"\r"


def command_description(command, hex_numbers=False):
    for number, name in [(3, "запрос состояния"), (5, "запрос версии ПО"),
                         (25, "запрос текущего уровня CH1")]:
        if command == command_code(number, hex_numbers):
            base = "HEX" if hex_numbers else "DEC"
            return f"Команда {number} ({base}, код 0x{command:02X}): {name}"
    return f"Неизвестная команда 0x{command:02X}"


def describe_request(packet, hex_numbers=False):
    if len(packet) < 3 or packet[:1] != b"&" or any(byte not in b"0123456789abcdefABCDEF" for byte in packet[1:3]):
        return "Нераспознанный запрос"
    command = int(packet[1:3], 16)
    text = command_description(command, hex_numbers)
    for mode in ("raw", "hex"):
        if packet == encode_request(command, mode):
            return text + f"; CRC {mode}: OK"
    return text + "; формат или CRC запроса не подтверждены"


def describe_reply(reply, hex_numbers=False):
    text = command_description(reply.command, hex_numbers)
    if reply.device_error:
        text += "; ОШИБКА: прибор отклонил команду"
    elif reply.command == command_code(5, hex_numbers):
        text += f"; ответ: версия {reply.data[0]}.{reply.data[1]}"
    elif reply.command == command_code(25, hex_numbers):
        value = int.from_bytes(reply.data, "little")
        text += f"; ответ: уровень {value} код = {value * 0.6105:.3f} мВ"
    elif reply.command == command_code(3, hex_numbers):
        state = reply.data[0]
        flags = [(1, "тревога"), (2, "неисправность"), (4, "обрыв кабеля"),
                 (8, "отключен"), (32, "КЗ кабеля"), (128, "дистанционный контроль")]
        names = [name for mask, name in flags if state & mask]
        if state & ~0xAF:
            names.append(f"неизвестные биты 0x{state & ~0xAF:02X}")
        text += f"; ответ: состояние 0x{state:02X} — " + (", ".join(names) or "норма")
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

    def __init__(self, lengths):
        self.lengths = lengths
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
            size = 0 if device_error else self.lengths[command]
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
            if self.buffer[end] == checksum and self.buffer[end + 1] == 13:
                mode, length = "raw", end + 2
            elif len(self.buffer) >= end + 3:
                crc_text = bytes(self.buffer[end:end + 2])
                if (all(byte in b"0123456789abcdefABCDEF" for byte in crc_text)
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
