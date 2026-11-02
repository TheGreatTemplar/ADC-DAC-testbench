"""ADC/DAC testbench with protocol capture and a read-only device channel."""

import argparse
import csv
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from serial.tools import list_ports
from protocol import ReplyParser, command_code, describe_reply, describe_request, encode_request
from transport import SerialWorker

CHANNEL_COLORS = ["#45d7d0", "#f4bd61", "#8fb6ff", "#e591c8",
                  "#a4d66f", "#ff927b", "#b6a0ef", "#d8e2ef"]
CHANNEL_COUNT = len(CHANNEL_COLORS)


class TestbenchWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADC / DAC Testbench — прототип")
        self.resize(1320, 820)
        self.setMinimumSize(1050, 700)
        self.rate = 1000
        self.sample_index = 0
        self.demo_logged_sample = 0
        self.running = False
        self.paused = False
        self.worker = None
        self.device_ready = False
        self.pending = None
        self.log_file = None
        self.source = "demo"
        self.session_start = time.monotonic()
        self.reply_parser = ReplyParser({})
        self.log_reply_parser = ReplyParser({})
        self.rng = np.random.default_rng()
        self.times = deque(maxlen=60000)
        self.values = [deque(maxlen=60000) for _ in range(CHANNEL_COUNT)]
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.build_menu()
        self.build_window()
        self.refresh_ports()
        self.port_timer = QtCore.QTimer(self)
        self.port_timer.setInterval(1500)
        self.port_timer.timeout.connect(self.refresh_ports)
        self.port_timer.start()
        self.request_timer = QtCore.QTimer(self)
        self.request_timer.setInterval(100)
        self.request_timer.timeout.connect(self.check_requests)
        self.request_timer.start()
        self.statusBar().showMessage("Готов к работе • Источник: демонстрационный сигнал")

    def build_menu(self):
        file_menu = self.menuBar().addMenu("Файл")
        self.export_action = file_menu.addAction("Сохранить буфер в CSV…", self.export_csv)
        self.export_action.setShortcut("Ctrl+S")
        file_menu.addAction("Показать журнал обмена", self.show_log)
        file_menu.addSeparator()
        file_menu.addAction("Выход", self.close, "Alt+F4")
        view_menu = self.menuBar().addMenu("Вид")
        view_menu.addAction("Автомасштаб", self.autoscale, "Ctrl+0")
        view_menu.addAction("Очистить график", self.clear_data)
        help_menu = self.menuBar().addMenu("Справка")
        help_menu.addAction("О программе", self.about)

    def build_window(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(24, 18, 24, 10)
        layout.setSpacing(16)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("ADC / DAC  <span style='color:#8191a8'>TESTBENCH</span>")
        title.setStyleSheet("font-size:25px; font-weight:700")
        header.addWidget(title)
        header.addStretch()
        self.badge = QtWidgets.QLabel("●  ДЕМО-РЕЖИМ")
        self.badge.setObjectName("badge")
        header.addWidget(self.badge)
        layout.addLayout(header)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        body_widget = QtWidgets.QWidget()
        body = QtWidgets.QHBoxLayout(body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        splitter.addWidget(body_widget)
        sidebar = QtWidgets.QWidget()
        sidebar.setMinimumWidth(260)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(14)
        self.sidebar_tabs = QtWidgets.QTabWidget()
        self.sidebar_tabs.setFixedWidth(290)
        port_page = QtWidgets.QWidget()
        port_layout = QtWidgets.QVBoxLayout(port_page)
        protocol_page = QtWidgets.QWidget()
        protocol_layout = QtWidgets.QVBoxLayout(protocol_page)
        self.sidebar_tabs.addTab(port_page, "Порт")
        self.sidebar_tabs.addTab(protocol_page, "Протокол")
        self.sidebar_tabs.addTab(sidebar, "Каналы")
        body.addWidget(self.sidebar_tabs)
        connection = QtWidgets.QGroupBox("ПОДКЛЮЧЕНИЕ")
        form = QtWidgets.QFormLayout(connection)
        form.setSpacing(10)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumWidth(130)
        form.addRow("COM-порт", self.port)
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems([str(rate) for rate in
                            [1200, 1800, 2400, 4800, 9600, 14400, 19200, 28800,
                             38400, 57600, 76800, 115200, 128000, 153600, 230400,
                             250000, 256000, 460800, 500000, 576000, 921600, 1000000]])
        self.baud.setCurrentText("9600")
        form.addRow("Скорость", self.baud)
        format_label = QtWidgets.QLabel("8 бит · без чётности · 1 стоп-бит")
        format_label.setObjectName("muted")
        form.addRow(format_label)
        refresh = QtWidgets.QPushButton("Обновить список портов")
        refresh.clicked.connect(self.refresh_ports)
        form.addRow(refresh)
        self.connect_button = QtWidgets.QPushButton("Подключить прибор")
        self.connect_button.clicked.connect(self.toggle_connection)
        form.addRow(self.connect_button)
        note = QtWidgets.QLabel("Список портов обновляется автоматически.\nПосле подключения запросы отправляются кнопками ниже.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)
        port_layout.addWidget(connection)
        port_layout.addStretch()
        protocol_group = QtWidgets.QGroupBox("ПРОТОКОЛ / ПРОБНЫЙ ОБМЕН")
        protocol_form = QtWidgets.QVBoxLayout(protocol_group)
        self.hex_commands = QtWidgets.QCheckBox("Номера команд в HEX")
        self.hex_commands.setToolTip("Снято: 25 → 0x19 (DEC). Установлено: 25 → 0x25 (HEX).")
        self.hex_commands.toggled.connect(self.reset_protocol)
        protocol_form.addWidget(self.hex_commands)
        self.crc_mode = QtWidgets.QComboBox()
        self.crc_mode.addItem("CRC запроса: 1 сырой байт", "raw")
        self.crc_mode.addItem("CRC запроса: 2 символа HEX", "hex")
        self.crc_mode.currentIndexChanged.connect(self.reset_protocol)
        protocol_form.addWidget(self.crc_mode)
        self.query_buttons = []
        for text, command in [("Версия ПО · 5", 5), ("Состояние · 3", 3), ("Текущий уровень · 25", 25)]:
            button = QtWidgets.QPushButton(text)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, number=command: self.query(number))
            protocol_form.addWidget(button)
            self.query_buttons.append(button)
        self.poll = QtWidgets.QCheckBox("Опрос уровня каждые 200 мс")
        self.poll.setEnabled(False)
        protocol_form.addWidget(self.poll)
        log_button = QtWidgets.QPushButton("Журнал TX / RX (HEX)")
        log_button.clicked.connect(self.show_log)
        protocol_form.addWidget(log_button)
        protocol_layout.addWidget(protocol_group)
        protocol_layout.addStretch()
        channels = QtWidgets.QGroupBox("КАНАЛЫ АЦП")
        channel_layout = QtWidgets.QGridLayout(channels)
        self.channel_checks = []
        for index, color in enumerate(CHANNEL_COLORS):
            check = QtWidgets.QCheckBox(f"CH{index + 1}")
            check.setChecked(index == 0)
            check.setEnabled(index == 0)
            check.setToolTip("Текущий уровень прибора" if index == 0 else "Резерв: не описан в протоколе")
            check.setStyleSheet(f"QCheckBox {{ color:{color}; font-weight:600; }} QCheckBox:disabled {{ color:#65758a; }}")
            check.toggled.connect(self.update_plot)
            self.channel_checks.append(check)
            channel_layout.addWidget(check, index // 2, index % 2)
        channel_layout.addWidget(QtWidgets.QLabel("CH1: уровень, код / мВ\nCH2–CH8: резерв"), 4, 0, 1, 2)
        side.addWidget(channels)
        acquisition = QtWidgets.QGroupBox("ОТОБРАЖЕНИЕ")
        acquisition_form = QtWidgets.QFormLayout(acquisition)
        self.window_seconds = QtWidgets.QComboBox()
        self.window_seconds.addItems(["2", "5", "10", "30", "60"])
        self.window_seconds.setCurrentText("10")
        self.window_seconds.currentTextChanged.connect(self.update_plot)
        acquisition_form.addRow("Окно, с", self.window_seconds)
        acquisition_form.addRow(QtWidgets.QLabel("Прибор: уровень по команде 25\nБуфер: последние 60 секунд"))
        side.addWidget(acquisition)
        dac_note = QtWidgets.QLabel("ЦАП\nУправление добавим после\nсогласования команд прибора.")
        dac_note.setObjectName("muted")
        side.addWidget(dac_note)
        side.addStretch()
        main = QtWidgets.QVBoxLayout()
        main.setSpacing(14)
        body.addLayout(main, 1)
        toolbar = QtWidgets.QHBoxLayout()
        self.start = QtWidgets.QPushButton("Запустить демо")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.toggle_run)
        toolbar.addWidget(self.start)
        self.pause = QtWidgets.QPushButton("Пауза графика")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.set_pause)
        toolbar.addWidget(self.pause)
        toolbar.addStretch()
        auto = QtWidgets.QPushButton("Автомасштаб")
        auto.clicked.connect(self.autoscale)
        toolbar.addWidget(auto)
        clear = QtWidgets.QPushButton("Очистить")
        clear.clicked.connect(self.clear_data)
        toolbar.addWidget(clear)
        main.addLayout(toolbar)
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background="#101b2b")
        self.plot.setMinimumHeight(150)
        self.plot.setLabel("left", "Уровень CH1, код")
        self.plot.setLabel("bottom", "Время", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(15, 10))
        self.plot.setYRange(1950, 2370)
        self.plot.setXRange(0, 10, padding=0)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.curves = [self.plot.plot(pen=pg.mkPen(color, width=1.5), name="CH1" if index == 0 else None)
                       for index, color in enumerate(CHANNEL_COLORS)]
        for curve in self.curves[1:]:
            curve.setVisible(False)
        main.addWidget(self.plot, 1)
        hint = QtWidgets.QLabel("Колесо мыши — масштаб · перетаскивание — перемещение · правая кнопка — меню графика")
        hint.setObjectName("muted")
        main.addWidget(hint)
        stats = QtWidgets.QHBoxLayout()
        self.stat_labels = []
        for caption in ["CH1 · ТЕКУЩЕЕ", "МИНИМУМ", "МАКСИМУМ", "РАЗМАХ P–P", "СКО"]:
            frame = QtWidgets.QFrame()
            frame.setObjectName("stat")
            box = QtWidgets.QVBoxLayout(frame)
            label = QtWidgets.QLabel(caption)
            label.setObjectName("muted")
            label.setStyleSheet("font-size:10px")
            value = QtWidgets.QLabel("—")
            value.setStyleSheet("font-size:23px; font-weight:600")
            box.addWidget(label)
            box.addWidget(value)
            stats.addWidget(frame)
            self.stat_labels.append(value)
        main.addLayout(stats)
        footer = QtWidgets.QHBoxLayout()
        self.buffer_label = QtWidgets.QLabel("Буфер пуст")
        self.buffer_label.setObjectName("muted")
        footer.addWidget(self.buffer_label)
        footer.addStretch()
        save = QtWidgets.QPushButton("Сохранить буфер CSV…")
        save.clicked.connect(self.export_csv)
        footer.addWidget(save)
        main.addLayout(footer)
        self.device_info = QtWidgets.QLabel("Прибор не подключён · 9600 8N1 · CH1 — средневыпрямленный уровень")
        self.device_info.setWordWrap(True)
        self.device_info.setObjectName("muted")
        main.addWidget(self.device_info)
        log_panel = QtWidgets.QGroupBox("ЖУРНАЛ ОБМЕНА · TX / RX · HEX")
        log_panel.setMinimumHeight(150)
        log_layout = QtWidgets.QVBoxLayout(log_panel)
        log_header = QtWidgets.QHBoxLayout()
        self.log_path = QtWidgets.QLabel("Файл журнала создаётся при подключении в папке captures.")
        self.log_path.setWordWrap(True)
        log_header.addWidget(self.log_path, 1)
        clear_log = QtWidgets.QPushButton("Очистить экран журнала")
        clear_log.setToolTip("Очищает только экран. Файл обмена с прибором сохраняется.")
        clear_log.clicked.connect(lambda: self.log_view.clear())
        log_header.addWidget(clear_log)
        log_layout.addLayout(log_header)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setStyleSheet("font-family:Consolas; font-size:12px")
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([510, 210])

    def show_log(self):
        self.log_view.setFocus()
        self.log_view.moveCursor(QtGui.QTextCursor.End)

    def reset_protocol(self, *_):
        if not hasattr(self, "hex_commands") or not hasattr(self, "crc_mode"):
            return
        self.reply_parser = ReplyParser({command_code(number, self.hex_commands.isChecked()): length
                                         for number, length in [(3, 1), (5, 2), (25, 2)]})
        self.log_reply_parser = ReplyParser(self.reply_parser.lengths)
        if self.log_file is not None:
            self.record("CONFIG", b"", time.monotonic())

    def set_request_controls(self):
        idle = self.pending is None
        for button in self.query_buttons:
            button.setEnabled(self.device_ready and idle)
        self.hex_commands.setEnabled(idle)
        self.crc_mode.setEnabled(idle)
        self.poll.setEnabled(self.device_ready)

    def toggle_connection(self):
        if self.worker is not None:
            self.poll.setChecked(False)
            self.device_ready = False
            self.worker.requestInterruption()
            self.connect_button.setEnabled(False)
            self.device_info.setText("Отключение порта…")
            self.set_request_controls()
            return
        port = self.port.currentData()
        if not port:
            QtWidgets.QMessageBox.information(self, "COM-порт", "Выберите доступный COM-порт.")
            return
        if self.running:
            self.toggle_run()
        directory = Path(__file__).resolve().parent / "captures"
        filename = "exchange_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".jsonl"
        try:
            directory.mkdir(exist_ok=True)
            capture_path = directory / filename
            self.log_file = capture_path.open("w", encoding="utf-8")
        except OSError:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Куда записать обмен с прибором?", filename,
                                                          "Журнал обмена (*.jsonl)")
            if not path:
                return
            try:
                capture_path = Path(path)
                self.log_file = capture_path.open("w", encoding="utf-8")
            except OSError as error:
                QtWidgets.QMessageBox.warning(self, "Журнал обмена", str(error))
                return
        self.source = "device"
        self.clear_data()
        self.reset_protocol()
        self.log_path.setText(str(capture_path))
        self.badge.setText("●  ПРИБОР · УРОВЕНЬ CH1")
        self.plot.enableAutoRange(axis="y", enable=True)
        self.worker = SerialWorker(port, int(self.baud.currentText()), self)
        self.worker.opened.connect(self.device_opened)
        self.worker.received.connect(self.receive_data)
        self.worker.transmitted.connect(self.data_transmitted)
        self.worker.failed.connect(self.device_failed)
        self.worker.finished.connect(self.device_finished)
        self.port.setEnabled(False)
        self.baud.setEnabled(False)
        self.start.setEnabled(False)
        self.connect_button.setText("Отключить прибор")
        self.device_info.setText(f"Открытие {port} · {self.baud.currentText()} 8N1…")
        self.worker.start()

    def device_opened(self):
        if self.worker is None or self.worker.isInterruptionRequested():
            return
        self.device_ready = True
        self.pending = None
        self.last_poll = time.monotonic()
        self.set_request_controls()
        self.device_info.setText("Порт открыт. Нажмите «Версия ПО» для первого запроса. Обмен записывается в журнал.")
        self.sidebar_tabs.setCurrentIndex(1)
        self.update_status()

    def device_failed(self, message):
        self.device_ready = False
        self.poll.setChecked(False)
        self.device_info.setText("Ошибка COM-порта: " + message)
        self.log_view.appendPlainText("ОШИБКА ПОРТА: " + message)
        self.set_request_controls()

    def device_finished(self):
        worker = self.worker
        self.worker = None
        self.device_ready = False
        self.pending = None
        self.poll.setChecked(False)
        self.set_request_controls()
        self.port.setEnabled(True)
        self.baud.setEnabled(True)
        self.start.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Подключить прибор")
        if self.log_file is not None:
            self.record("CLOSE", b"", time.monotonic())
            self.log_file.close()
            self.log_file = None
        self.log_view.appendPlainText("Порт закрыт. Журнал сохранён.")
        if worker is not None:
            worker.deleteLater()
        self.update_status()

    def record(self, direction, data, stamp, simulated=False):
        utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        description = ""
        if direction in ("TX", "RX"):
            hex_numbers = self.hex_commands.isChecked()
            if direction == "TX":
                description = describe_request(data, hex_numbers)
            else:
                parser = (ReplyParser({command_code(number, hex_numbers): length
                                       for number, length in [(3, 1), (5, 2), (25, 2)]})
                          if simulated else self.log_reply_parser)
                replies = parser.feed(data)
                description = " | ".join(describe_reply(reply, hex_numbers) for reply in replies)
                if not description:
                    description = "Фрагмент ответа; ожидается продолжение" if parser.buffer else "Формат ответа или CRC не распознаны"
            source = "[ДЕМО] " if simulated else "[ПРИБОР] "
            self.log_view.appendPlainText(f"{utc} {source}{direction}  {data.hex(' ').upper()}  | {description}")
        if self.log_file is not None and not simulated:
            record = dict(utc=utc, monotonic_s=stamp, direction=direction, hex=data.hex(" ").upper(),
                          command_numbers="HEX" if self.hex_commands.isChecked() else "DEC",
                          tx_crc=self.crc_mode.currentData(), port=self.port.currentData(),
                          baud=int(self.baud.currentText()), serial_format="8N1", description=description)
            try:
                self.log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.log_file.flush()
            except OSError as error:
                self.log_file.close()
                self.log_file = None
                self.poll.setChecked(False)
                self.log_path.setText("ОШИБКА записи журнала: " + str(error))
                self.device_info.setText("Запись обмена остановлена: " + str(error))

    def query(self, number):
        if not self.device_ready or self.pending is not None or number not in (3, 5, 25):
            return
        code = command_code(number, self.hex_commands.isChecked())
        self.pending = dict(number=number, code=code, deadline=time.monotonic() + 1.0)
        self.last_poll = time.monotonic()
        self.set_request_controls()
        self.worker.send(encode_request(code, self.crc_mode.currentData()))
        self.device_info.setText(f"Ожидается ответ: команда {number}, байт 0x{code:02X}…")

    def data_transmitted(self, data, stamp):
        self.record("TX", data, stamp)
        if self.pending is not None:
            self.pending["deadline"] = stamp + 0.5

    def check_requests(self):
        now = time.monotonic()
        if self.pending is not None and now >= self.pending["deadline"]:
            code = self.pending["code"]
            self.pending = None
            self.poll.setChecked(False)
            self.set_request_controls()
            message = f"Нет корректного ответа на 0x{code:02X} за 500 мс. Проверьте журнал и вариант CRC / DEC / HEX."
            self.device_info.setText(message)
            self.log_view.appendPlainText(message)
        if self.device_ready and self.poll.isChecked() and self.pending is None and now - self.last_poll >= 0.2:
            self.query(25)

    def receive_data(self, data, stamp):
        self.record("RX", data, stamp)
        previous = self.reply_parser.rejected
        for reply in self.reply_parser.feed(data):
            if self.pending is not None and reply.command == self.pending["code"]:
                self.pending = None
                self.set_request_controls()
            if reply.device_error:
                self.poll.setChecked(False)
                self.device_info.setText(f"Прибор отклонил команду 0x{reply.command:02X}; CRC {reply.checksum_mode} верна.")
                continue
            if reply.command == command_code(5, self.hex_commands.isChecked()):
                text = f"Версия ПО: {reply.data[0]}.{reply.data[1]}"
            elif reply.command == command_code(3, self.hex_commands.isChecked()):
                state = reply.data[0]
                flags = [(1, "тревога"), (2, "неисправность"), (4, "обрыв кабеля"),
                         (8, "отключен"), (32, "КЗ кабеля"), (128, "дистанционный контроль")]
                text = f"Состояние: 0x{state:02X} · " + (", ".join(name for mask, name in flags if state & mask) or "норма")
                if state & ~0xAF:
                    text += " · неизвестные биты"
            else:
                value = int.from_bytes(reply.data, "little")
                self.add_device_sample(value, stamp)
                text = f"Уровень: {value} код · {value * 0.6105:.3f} мВ"
            self.device_info.setText(text + f" · CRC {reply.checksum_mode}: OK")
        if self.reply_parser.rejected != previous:
            self.log_view.appendPlainText("Ответ не распознан или CRC не совпала. Исходные байты сохранены.")

    def add_device_sample(self, value, stamp):
        self.times.append(max(0.0, stamp - self.session_start))
        self.values[0].append(value)
        while self.times and self.times[0] < self.times[-1] - 60:
            self.times.popleft()
            self.values[0].popleft()
        self.buffer_label.setText(f"Прибор · {len(self.times)} измерений · уровень по команде 25")
        if not self.paused:
            self.update_plot()

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.requestInterruption()
            if not self.worker.wait(1500):
                self.device_info.setText("Ожидается завершение операции COM-порта. Повторите закрытие.")
                event.ignore()
                return
            self.device_finished()
        event.accept()

    def refresh_ports(self):
        if self.worker is not None:
            return
        if self.port.view().isVisible():
            return
        selected = self.port.currentData()
        try:
            ports = sorted(list_ports.comports(), key=lambda item: item.device)
        except OSError:
            return
        devices = [item.device for item in ports]
        current = [self.port.itemData(index) for index in range(self.port.count())]
        if current == (devices or [None]):
            return
        self.port.blockSignals(True)
        self.port.clear()
        for item in ports:
            self.port.addItem(item.device, item.device)
            self.port.setItemData(self.port.count() - 1, item.description, QtCore.Qt.ToolTipRole)
        if self.port.count() == 0:
            self.port.addItem("Нет доступных портов", None)
        else:
            index = self.port.findData(selected)
            self.port.setCurrentIndex(max(0, index))
        self.port.blockSignals(False)

    def toggle_run(self):
        if self.worker is not None:
            return
        if self.source != "demo":
            self.source = "demo"
            self.clear_data()
            self.badge.setText("●  ДЕМО-РЕЖИМ")
        self.running = not self.running
        if self.running:
            self.timer.start()
        else:
            self.timer.stop()
        self.start.setText("Остановить демо" if self.running else "Запустить демо")
        self.update_status()

    def update_status(self):
        if self.source == "device":
            message = "Прибор подключён" if self.device_ready else "Прибор отключён"
            if self.paused:
                message += " • Пауза графика, приём продолжается"
            self.statusBar().showMessage(message)
            return
        message = "Демо: приём идёт" if self.running else "Демо остановлено"
        if self.paused:
            message += " • График на паузе; буфер продолжает обновляться при работающем демо"
        self.statusBar().showMessage(message)

    def set_pause(self, checked):
        self.paused = checked
        self.pause.setText("Продолжить график" if checked else "Пауза графика")
        if not checked:
            self.update_plot()
        self.update_status()

    def tick(self):
        count = self.rate * self.timer.interval() // 1000
        times = np.arange(self.sample_index, self.sample_index + count) / self.rate
        self.sample_index += count
        signals = [2048 + 23 * np.sin(2 * np.pi * 1.2 * times) + self.rng.normal(0, 5, count)]
        self.times.extend(times)
        for buffer, signal in zip(self.values, signals):
            buffer.extend(np.clip(np.rint(signal), 0, 4095).astype(int))
        if self.sample_index - self.demo_logged_sample >= self.rate // 5:
            self.demo_logged_sample = self.sample_index
            self.demo_exchange(int(self.values[0][-1]))
        self.buffer_label.setText(f"В буфере: {len(self.times):,} отсчётов/канал · демо · 1 000 отсчётов/с".replace(",", " "))
        if not self.paused:
            self.update_plot()

    def demo_exchange(self, value):
        command = command_code(25, self.hex_commands.isChecked())
        mode = self.crc_mode.currentData()
        data = value.to_bytes(2, "little")
        checksum = (command + sum(data)) & 255
        crc = bytes([checksum]) if mode == "raw" else f"{checksum:02X}".encode("ascii")
        reply = b"!" + f"{command:02X}".encode("ascii") + data.hex().upper().encode("ascii") + crc + b"\r"
        stamp = time.monotonic()
        self.record("TX", encode_request(command, mode), stamp, simulated=True)
        self.record("RX", reply, stamp, simulated=True)

    def update_plot(self, *_):
        if not hasattr(self, "curves"):
            return
        for curve, check in zip(self.curves, self.channel_checks):
            curve.setVisible(check.isEnabled() and check.isChecked())
        if not self.times or self.paused:
            return
        times = np.asarray(self.times)
        seconds = float(self.window_seconds.currentText())
        right = max(seconds, times[-1])
        left = right - seconds
        start = np.searchsorted(times, left)
        for curve, buffer in zip(self.curves, self.values):
            if buffer:
                curve.setData(times[start:], np.asarray(buffer)[start:])
        self.plot.setXRange(left, right, padding=0)
        values = np.asarray(self.values[0])[start:]
        if len(values):
            numbers = [f"{values[-1]:.0f}", f"{values.min():.0f}", f"{values.max():.0f}",
                       f"{np.ptp(values):.0f}", f"{np.std(values):.2f}"]
            for label, number in zip(self.stat_labels, numbers):
                label.setText(number)

    def autoscale(self):
        self.plot.enableAutoRange(axis="y", enable=True)
        self.update_plot()

    def clear_data(self):
        self.times.clear()
        for buffer, curve in zip(self.values, self.curves):
            buffer.clear()
            curve.setData([], [])
        self.sample_index = 0
        self.demo_logged_sample = 0
        self.session_start = time.monotonic()
        for label in self.stat_labels:
            label.setText("—")
        self.buffer_label.setText("Буфер пуст")
        self.plot.setXRange(0, float(self.window_seconds.currentText()), padding=0)

    def write_csv(self, path):
        with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["source", "time_s", "ch1_level_code", "ch1_level_mV"])
            writer.writerows((self.source, f"{stamp:.6f}", value, f"{value * 0.6105:.4f}")
                             for stamp, value in zip(self.times, self.values[0]))

    def export_csv(self):
        if not self.times:
            QtWidgets.QMessageBox.information(self, "Сохранение", "Буфер пуст. Запустите демо или запросите уровень прибора.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить буфер", f"adc_{self.source}.csv", "CSV (*.csv)")
        if path:
            try:
                self.write_csv(path)
                self.statusBar().showMessage(f"Сохранено: {path}")
            except OSError as error:
                QtWidgets.QMessageBox.warning(self, "Ошибка сохранения", str(error))

    def about(self):
        QtWidgets.QMessageBox.about(self, "О программе", "ADC / DAC Testbench\nВерсия 0.3 — пробный обмен\n\n"
                                  "CH1 — уровень сигнала (команда 25). CH2–CH8 — резерв.\n"
                                  "Формат CRC и система счисления команд проверяются по журналу.\n"
                                  "Статистика CH1 рассчитана по видимому временному окну.\nСКО — отклонение от среднего значения.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="Render demo and save preview.png")
    args = parser.parse_args()
    app = QtWidgets.QApplication(sys.argv[:1])
    if sys.platform == "win32":
        import os

        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ["segoeui.ttf", "segoeuib.ttf"]:
            QtGui.QFontDatabase.addApplicationFont(str(font_dir / filename))
        app.setFont(QtGui.QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background:#111b2b; color:#dce6f3; font-family:'Segoe UI'; font-size:12px; }
        QMainWindow, QMenuBar, QStatusBar { background:#111b2b; }
        QGroupBox { background:#172437; border:1px solid #29394e; border-radius:8px;
                    margin-top:13px; padding:18px 12px 12px; font-weight:600; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; color:#8ca1bc; }
        QLabel { background:transparent; }
        QPushButton, QComboBox { background:#1e3048; border:1px solid #354862;
                                border-radius:5px; padding:8px 10px; }
        QComboBox { min-height:18px; }
        QTabWidget::pane { border:1px solid #29394e; }
        QTabBar::tab { background:#172437; padding:9px 12px; color:#91a3bb; }
        QTabBar::tab:selected { background:#1e3048; color:#45d7d0; }
        QPushButton:hover { background:#2b4260; border-color:#5890b0; }
        QPushButton:checked { background:#395370; }
        QPushButton:disabled { color:#738197; background:#1a2637; border-color:#29364a; }
        QPushButton#primary { background:#167e85; border-color:#25a9ae; color:white; font-weight:600; }
        QPushButton#primary:hover { background:#20969d; }
        QComboBox QAbstractItemView { background:#1e3048; selection-background-color:#167e85; }
        QLabel#muted { color:#91a3bb; }
        QLabel#badge { background:#15383e; color:#5de0d4; border-radius:5px; padding:8px 14px; font-weight:600; }
        QFrame#stat { background:#172437; border:1px solid #29394e; border-radius:7px; }
        QMenu { background:#1e3048; }
        QMenu::item:selected { background:#167e85; }
        QStatusBar { color:#91a3bb; border-top:1px solid #29394e; }
    """)
    window = TestbenchWindow()
    window.show()
    if args.preview:
        for _ in range(220):
            window.tick()
        app.processEvents()
        window.grab().save(str(Path.cwd() / "preview.png"))
        return 0
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
