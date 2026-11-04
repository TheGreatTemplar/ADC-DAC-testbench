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
from protocol import (ADC_CHANNEL_NAMES, ADC_CSV_COLUMNS, FW018_LENGTHS, AdcStreamParser, ReplyParser,
                      command_code, decode_working_snapshot, describe_adc_sample, describe_device_state, describe_reply,
                      describe_request, encode_request)
from transport import SerialWorker
from swd import ThresholdSnapshot, ThresholdWorker
from battery import BatteryBatchFilter

CHANNEL_COLORS = ["#45d7d0", "#f4bd61", "#8fb6ff", "#e591c8",
                  "#a4d66f", "#ff927b", "#b6a0ef", "#d8e2ef"]
CHANNEL_COUNT = len(CHANNEL_COLORS)
CH2_MV_PER_CODE = 3300.0 / 4095
BATTERY_VREF_V = 3.3
BATTERY_R_TOP_KOHM = 22.0
BATTERY_R_BOTTOM_KOHM = 42.2
BATTERY_V_PER_CODE = BATTERY_VREF_V / 4095 * (BATTERY_R_TOP_KOHM + BATTERY_R_BOTTOM_KOHM) / BATTERY_R_BOTTOM_KOHM


def application_directory():
    # One-file bundles unpack __file__ to a temporary directory.
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


class Ch2MillivoltAxis(pg.AxisItem):
    """Secondary labels for the shared ADC-code coordinate system."""

    def tickStrings(self, values, scale, spacing):
        return [f"{value * CH2_MV_PER_CODE:.1f}" for value in values]


class BatteryVoltageAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [f"{value * BATTERY_V_PER_CODE:.3f}" for value in values]


class TestbenchWindow(QtWidgets.QMainWindow):
    def __init__(self, swd_auto_start=True):
        super().__init__()
        self.setWindowTitle("ADC / DAC Testbench — тестовая / v0.18")
        self.resize(1320, 820)
        self.setMinimumSize(1050, 700)
        self.rate = 1000
        self.sample_index = 0
        self.demo_logged_sample = 0
        self.running = False
        self.paused = False
        self.worker = None
        self.threshold_worker = None
        self.threshold_reads = 0
        self.swd_auto_start = swd_auto_start
        self.threshold_snapshot = None
        self.threshold_times = deque(maxlen=60000)
        self.threshold_signals = deque(maxlen=60000)
        self.threshold_plot_visible = False
        self.threshold_legend_state = None
        self.last_uart_snapshot = None
        self.last_swd_snapshot = None
        self.last_swd_stamp = 0
        self.last_noise = None
        self.last_state_stamp = 0
        self.gain_read_needed = False
        self.gain_requested = None
        self.queued_gain = None
        self.queued_query = None
        self.working_thresholds = deque(maxlen=60000)
        self.working_states = deque(maxlen=60000)
        self.device_ready = False
        self.pending = None
        self.log_file = None
        self.source = "demo"
        self.session_start = time.monotonic()
        self.reply_parser = ReplyParser({})
        self.log_reply_parser = ReplyParser({})
        self.adc_parser = AdcStreamParser()
        self.log_adc_parser = AdcStreamParser()
        self.buffer_format = "protocol"
        self.rng = np.random.default_rng()
        self.times = deque(maxlen=60000)
        self.values = [deque(maxlen=60000) for _ in range(CHANNEL_COUNT)]
        self.channel_filters = [BatteryBatchFilter() for _ in range(CHANNEL_COUNT)]
        self.filtered_values = [deque(maxlen=60000) for _ in range(CHANNEL_COUNT)]
        self.battery_filter = self.channel_filters[2]
        self.filtered_battery_values = self.filtered_values[2]
        self.alarm_filter = self.channel_filters[1]
        self.filtered_alarm_values = self.filtered_values[1]
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.build_menu()
        self.build_window()
        self.change_receive_mode()
        self.configure_channels()
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
        filters_page = QtWidgets.QWidget()
        filters_layout = QtWidgets.QVBoxLayout(filters_page)
        self.sidebar_tabs.addTab(filters_page, "Фильтры")
        body.addWidget(self.sidebar_tabs)
        connection = QtWidgets.QGroupBox("ПОДКЛЮЧЕНИЕ")
        form = QtWidgets.QFormLayout(connection)
        form.setSpacing(6)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumWidth(130)
        form.addRow("COM-порт", self.port)
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems([str(rate) for rate in
                            [1200, 1800, 2400, 4800, 9600, 14400, 19200, 28800,
                             38400, 57600, 76800, 115200, 128000, 153600, 230400,
                             250000, 256000, 460800, 500000, 576000, 921600, 1000000]])
        self.baud.setCurrentText("19200")
        form.addRow("Скорость", self.baud)
        self.receive_mode = QtWidgets.QComboBox()
        self.receive_mode.addItem("Рабочая v0.18", "fw018")
        self.receive_mode.addItem("Тестовая · АЦП A0–A2", "adc_stream")
        self.receive_mode.addItem("Протокол 0.0 (вручную)", "protocol")
        self.receive_mode.setToolTip("v0.18: сигнал и порог по UART. Тестовая: поток A0/A1/A2 без CRC.")
        self.receive_mode.currentIndexChanged.connect(self.change_receive_mode)
        form.addRow("Формат", self.receive_mode)
        self.data_bits = QtWidgets.QComboBox()
        for bits in (5, 6, 7, 8):
            self.data_bits.addItem(str(bits), bits)
        self.data_bits.setCurrentIndex(self.data_bits.findData(8))
        form.addRow("Биты данных", self.data_bits)
        self.parity = QtWidgets.QComboBox()
        for label, value in [("Нет", "N"), ("Чётная", "E"), ("Нечётная", "O"),
                             ("Маркер (1)", "M"), ("Пробел (0)", "S")]:
            self.parity.addItem(label, value)
        form.addRow("Чётность", self.parity)
        self.stop_bits = QtWidgets.QComboBox()
        for bits in (1, 1.5, 2):
            self.stop_bits.addItem(f"{bits:g}", bits)
        form.addRow("Стоп-биты", self.stop_bits)
        refresh = QtWidgets.QPushButton("Обновить список портов")
        refresh.clicked.connect(self.refresh_ports)
        form.addRow(refresh)
        self.connect_button = QtWidgets.QPushButton("Подключить прибор")
        self.connect_button.clicked.connect(self.toggle_connection)
        form.addRow(self.connect_button)
        note = QtWidgets.QLabel("v0.18 и тестовая: 19200, 8N1.\nВыберите установленную прошивку.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)
        port_layout.addWidget(connection)
        port_layout.addStretch()
        protocol_group = QtWidgets.QGroupBox("ОБМЕН С ПРИБОРОМ")
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
        query_grid = QtWidgets.QGridLayout()
        for text, command in [("Версия ПО · 5", 5), ("Состояние · 3", 3), ("Уровень · 25", 25),
                              ("Сигнал · 11", 11)]:
            button = QtWidgets.QPushButton(text)
            button.setEnabled(False)
            button.setProperty("command", command)
            button.setToolTip(f"Команда {command}" + (": сигнал, порог и состояние v0.18" if command == 11 else ""))
            button.clicked.connect(lambda checked=False, number=command: self.query(number))
            index = len(self.query_buttons)
            query_grid.addWidget(button, index // 2, index % 2)
            self.query_buttons.append(button)
        protocol_form.addLayout(query_grid)
        self.poll = QtWidgets.QCheckBox("Опрос уровня каждые 200 мс")
        self.poll.setEnabled(False)
        protocol_form.addWidget(self.poll)
        self.gain_group = gain_group = QtWidgets.QGroupBox("УСИЛЕНИЕ · v0.18")
        gain_form = QtWidgets.QVBoxLayout(gain_group)
        self.gain_info = QtWidgets.QLabel("Текущая ступень: —")
        self.gain_info.setWordWrap(True)
        gain_form.addWidget(self.gain_info)
        gain_grid = QtWidgets.QGridLayout()
        self.gain_buttons = []
        self.gain_button_group = QtWidgets.QButtonGroup(self)
        self.gain_button_group.setExclusive(True)
        for step in range(1, 9):
            button = QtWidgets.QPushButton(str(step))
            button.setCheckable(True)
            button.setToolTip(f"Установить ступень усиления {step}; значение проверяется чтением")
            button.setStyleSheet("QPushButton:checked { background:#167e85; border-color:#45d7d0; color:white; }")
            button.clicked.connect(lambda checked=False, value=step: self.select_gain(value))
            self.gain_button_group.addButton(button, step)
            self.gain_buttons.append(button)
            gain_grid.addWidget(button, (step - 1) // 4, (step - 1) % 4)
        gain_form.addLayout(gain_grid)
        self.gain_read = QtWidgets.QPushButton("Прочитать усиление")
        self.gain_read.clicked.connect(lambda: self.query(15))
        gain_form.addWidget(self.gain_read)
        protocol_form.addWidget(gain_group)
        self.read_thresholds = QtWidgets.QCheckBox("Пороги через ST-Link / SWD")
        self.read_thresholds.setToolTip("Читает ОЗУ выбранной прошивки через HOTPLUG. Для v0.18 порог срабатывания доступен и по UART.")
        self.read_thresholds.toggled.connect(self.toggle_thresholds)
        protocol_form.addWidget(self.read_thresholds)
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
        self.channel_note = QtWidgets.QLabel("Поток АЦП:\n" + "\n".join(
            f"CH{index + 1} / A{index}: {name}" for index, name in enumerate(ADC_CHANNEL_NAMES)))
        self.channel_note.setWordWrap(True)
        channel_layout.addWidget(self.channel_note, 4, 0, 1, 2)
        side.addWidget(channels)
        acquisition = QtWidgets.QGroupBox("ОТОБРАЖЕНИЕ")
        acquisition_form = QtWidgets.QFormLayout(acquisition)
        filter_group = QtWidgets.QGroupBox("ФИЛЬТРЫ КАНАЛОВ")
        filter_form = QtWidgets.QFormLayout(filter_group)
        filter_form.setSpacing(6)
        self.filter_checks = []
        self.filter_size_groups = {}
        self.filter_size_widgets = {}
        for channel in range(CHANNEL_COUNT):
            check = QtWidgets.QCheckBox(f"CH{channel + 1}")
            check.setChecked(channel == 2)
            check.setStyleSheet(f"QCheckBox {{ color:{CHANNEL_COLORS[channel]}; font-weight:600; }}")
            self.filter_checks.append(check)
            check.setToolTip("Отбросить по 10% крайних значений, усреднить остальные. Размер — в отсчётах. Исходные данные сохраняются.")
            check.toggled.connect(self.update_plot)
            group = QtWidgets.QButtonGroup(self)
            group.setExclusive(True)
            field = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            for size in (256, 128, 64, 32):
                button = QtWidgets.QPushButton(str(size))
                button.setCheckable(True)
                button.setChecked(size == 256)
                button.setStyleSheet("QPushButton { padding:6px 3px; } QPushButton:checked { background:#167e85; border-color:#45d7d0; color:white; }"
                                    "QPushButton:checked:disabled { background:#233848; border-color:#354862; color:#738197; }")
                button.setToolTip("Размер массива; при смене фильтр пересчитывается по исходным данным буфера")
                button.clicked.connect(lambda checked=False, ch=channel, count=size: self.set_filter_size(ch, count))
                group.addButton(button, size)
                row.addWidget(button)
            self.filter_size_groups[channel] = group
            self.filter_size_widgets[channel] = field
            filter_form.addRow(check, field)
        self.alarm_filter_check = self.filter_checks[1]
        self.battery_filter_check = self.filter_checks[2]
        filters_layout.addWidget(filter_group)
        filter_note = QtWidgets.QLabel("Галочка — включить фильтр. Кнопки — размер массива в отсчётах.\n"
                                      "Усреднение с отбрасыванием по 10% крайних значений. Исходные данные сохраняются.\n"
                                      "Неактивные каналы не передаются выбранным источником.")
        filter_note.setWordWrap(True)
        filter_note.setObjectName("muted")
        filters_layout.addWidget(filter_note)
        filters_layout.addStretch()
        self.window_seconds = 60
        self.window_button_group = QtWidgets.QButtonGroup(self)
        self.window_button_group.setExclusive(True)
        self.window_buttons = []
        window_row = QtWidgets.QHBoxLayout()
        window_row.setSpacing(3)
        for seconds in (5, 10, 30, 60):
            button = QtWidgets.QPushButton(str(seconds))
            button.setCheckable(True)
            button.setChecked(seconds == self.window_seconds)
            button.setStyleSheet("QPushButton { padding:6px 4px; } QPushButton:checked { background:#167e85; border-color:#45d7d0; color:white; }")
            button.clicked.connect(lambda checked=False, value=seconds: self.set_window_seconds(value))
            self.window_button_group.addButton(button, seconds)
            self.window_buttons.append(button)
            window_row.addWidget(button)
        acquisition_form.addRow("Окно, с", window_row)
        acquisition_form.addRow(QtWidgets.QLabel("Время прибора — по приёму ПК\nБуфер: последние 60 секунд"))
        side.addWidget(acquisition)
        dac_note = QtWidgets.QLabel("ЦАП\nУправление добавим после\nсогласования команд прибора.")
        dac_note.setObjectName("muted")
        side.addWidget(dac_note)
        side.addStretch()
        main = QtWidgets.QVBoxLayout()
        main.setSpacing(4)
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
        auto = QtWidgets.QPushButton("Автомасштаб")
        auto.clicked.connect(self.autoscale)
        toolbar.addWidget(auto)
        clear = QtWidgets.QPushButton("Очистить")
        clear.clicked.connect(self.clear_data)
        toolbar.addWidget(clear)
        toolbar.addStretch()
        self.battery_readout = QtWidgets.QLabel("CH3: — В")
        self.battery_readout.setStyleSheet(f"color:{CHANNEL_COLORS[2]}; font-size:18px; font-weight:600")
        self.battery_readout.setToolTip("Питание: код × 3,3 / 4095 × (22 + 42,2) / 42,2. Значения резисторов в кОм.")
        self.battery_readout.setVisible(False)
        toolbar.addWidget(self.battery_readout)
        main.addLayout(toolbar)
        pg.setConfigOptions(antialias=True)
        self.ch2_mv_axis = Ch2MillivoltAxis(orientation="right")
        self.ch2_mv_axis.setLabel("CH2, мВ · опора 3,3 В", color=CHANNEL_COLORS[1])
        self.ch2_mv_axis.setTextPen(CHANNEL_COLORS[1])
        self.ch2_mv_axis.enableAutoSIPrefix(False)
        self.ch2_mv_axis.setWidth(60)
        self.ch2_mv_axis.setStyle(tickTextOffset=3)
        self.plot = pg.PlotWidget(background="#101b2b", axisItems={"right": self.ch2_mv_axis})
        self.plot.showAxis("right", False)
        self.plot.setMinimumHeight(150)
        self.plot.plotItem.layout.setHorizontalSpacing(1)
        self.plot.setLabel("left", "Уровень CH1, код")
        self.plot.setLabel("bottom", "Время", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        legend = self.plot.addLegend(offset=(15, 10))
        legend.setColumnCount(2)
        legend.setZValue(1000)
        self.plot.setYRange(1950, 2370)
        self.plot.setXRange(0, self.window_seconds, padding=0)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.curves = [self.plot.plot(pen=pg.mkPen(color, width=1.5), name="CH1" if index == 0 else None)
                       for index, color in enumerate(CHANNEL_COLORS)]
        for curve in self.curves[1:]:
            curve.setVisible(False)
        self.battery_axis = BatteryVoltageAxis(orientation="right")
        self.battery_axis.setLabel("CH3 · питание, В", color=CHANNEL_COLORS[2])
        self.battery_axis.setTextPen(CHANNEL_COLORS[2])
        self.battery_axis.enableAutoSIPrefix(False)
        self.battery_axis.setWidth(58)
        self.battery_axis.setStyle(tickTextOffset=3)
        self.battery_axis.linkToView(self.plot.getViewBox())
        self.plot.plotItem.layout.addItem(self.battery_axis, 2, 3)
        self.battery_axis.hide()
        self.threshold_axis = pg.AxisItem(orientation="right")
        self.threshold_axis.setLabel("Обработка / пороги, ед.", color="#e591c8")
        self.threshold_axis.setTextPen("#e591c8")
        self.threshold_axis.enableAutoSIPrefix(False)
        self.threshold_axis.setWidth(50)
        self.threshold_axis.setStyle(tickTextOffset=3)
        self.plot.plotItem.layout.addItem(self.threshold_axis, 2, 4)
        self.threshold_view = pg.ViewBox()
        self.plot.scene().addItem(self.threshold_view)
        self.threshold_axis.linkToView(self.threshold_view)
        self.threshold_view.setXLink(self.plot.getViewBox())
        self.threshold_signal_curve = pg.PlotDataItem(pen=pg.mkPen("#e591c8", width=1))
        self.threshold_view.addItem(self.threshold_signal_curve)
        self.trigger_lines = [pg.InfiniteLine(angle=0, pen=pg.mkPen("#ff927b", width=1)) for _ in range(2)]
        self.release_lines = [pg.InfiniteLine(angle=0, pen=pg.mkPen("#8fb6ff", width=1, style=QtCore.Qt.DashLine))
                              for _ in range(2)]
        for line in self.trigger_lines + self.release_lines:
            self.threshold_view.addItem(line)
        self.threshold_legend_samples = [pg.PlotDataItem(pen=line.pen) for line in
                                         (self.trigger_lines[0], self.release_lines[0])]
        self.threshold_axis.hide()
        self.threshold_view.hide()
        self.plot.getViewBox().sigResized.connect(self.sync_threshold_view)
        self.sync_threshold_view()
        main.addWidget(self.plot, 1)
        self.plot.setToolTip("Колесо мыши — масштаб · перетаскивание — перемещение · правая кнопка — меню графика")
        stats = QtWidgets.QHBoxLayout()
        stats.setSpacing(6)
        self.stat_frames = []
        self.stat_labels = []
        self.stat_ch2_labels = []
        self.stat_ch3_labels = []
        for caption in ["ТЕКУЩЕЕ", "МИНИМУМ", "МАКСИМУМ", "РАЗМАХ P–P", "СКО"]:
            frame = QtWidgets.QFrame()
            frame.setObjectName("stat")
            frame.setFixedHeight(36)
            box = QtWidgets.QVBoxLayout(frame)
            box.setContentsMargins(8, 1, 8, 1)
            box.setSpacing(0)
            label = QtWidgets.QLabel(caption)
            label.setObjectName("muted")
            label.setStyleSheet("font-size:9px")
            value = QtWidgets.QLabel("CH1 —")
            value.setStyleSheet(f"font-size:12px; font-weight:600; color:{CHANNEL_COLORS[0]}")
            value.setToolTip("CH1: коды АЦП по видимому окну")
            alarm_value = QtWidgets.QLabel("CH2 —")
            alarm_value.setStyleSheet(f"font-size:12px; font-weight:600; color:{CHANNEL_COLORS[1]}")
            alarm_value.setToolTip("CH2: исходные коды АЦП по видимому окну")
            alarm_value.setVisible(False)
            battery_value = QtWidgets.QLabel("CH3 — В")
            battery_value.setStyleSheet(f"font-size:12px; font-weight:600; color:{CHANNEL_COLORS[2]}")
            battery_value.setToolTip("CH3: напряжение питания в вольтах по видимому окну")
            battery_value.setVisible(False)
            numbers = QtWidgets.QGridLayout()
            numbers.setHorizontalSpacing(6)
            numbers.setVerticalSpacing(0)
            numbers.addWidget(value, 0, 0)
            numbers.addWidget(alarm_value, 0, 1)
            numbers.addWidget(battery_value, 1, 0, 1, 2)
            box.addWidget(label)
            box.addLayout(numbers)
            stats.addWidget(frame)
            self.stat_frames.append(frame)
            self.stat_labels.append(value)
            self.stat_ch2_labels.append(alarm_value)
            self.stat_ch3_labels.append(battery_value)
        main.addLayout(stats)
        footer = QtWidgets.QHBoxLayout()
        self.buffer_label = QtWidgets.QLabel("Буфер пуст")
        self.buffer_label.setObjectName("muted")
        footer.addWidget(self.buffer_label)
        footer.addStretch()
        self.ch2_readout = QtWidgets.QLabel("CH2: — код / — мВ")
        self.ch2_readout.setStyleSheet(f"color:{CHANNEL_COLORS[1]}")
        self.ch2_readout.setVisible(False)
        footer.addWidget(self.ch2_readout)
        main.addLayout(footer)
        self.device_info = QtWidgets.QLabel("Прибор не подключён · поток A0–A2: 19200 8N1, без CRC")
        self.device_info.setWordWrap(True)
        self.device_info.setObjectName("muted")
        main.addWidget(self.device_info)
        self.device_state_info = QtWidgets.QLabel("Диагностика кабеля: нет данных")
        self.device_state_info.setWordWrap(True)
        main.addWidget(self.device_state_info)
        self.threshold_info = QtWidgets.QLabel("Пороги: чтение через ST-Link выключено")
        self.threshold_info.setWordWrap(True)
        self.threshold_info.setObjectName("muted")
        self.threshold_info.setStyleSheet(f"color:{CHANNEL_COLORS[1]}")
        main.addWidget(self.threshold_info)
        log_panel = QtWidgets.QGroupBox("ЖУРНАЛ ОБМЕНА · TX / RX · HEX")
        log_panel.setMinimumHeight(150)
        log_layout = QtWidgets.QVBoxLayout(log_panel)
        log_header = QtWidgets.QHBoxLayout()
        self.log_path = QtWidgets.QLabel("Файл журнала создаётся при подключении в папке captures.")
        self.log_path.setWordWrap(True)
        log_header.addWidget(self.log_path, 1)
        save = QtWidgets.QPushButton("Сохранить буфер CSV…")
        save.clicked.connect(self.export_csv)
        log_header.addWidget(save)
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

    def set_window_seconds(self, seconds):
        if seconds not in (5, 10, 30, 60):
            return
        self.window_seconds = seconds
        self.window_button_group.button(seconds).setChecked(True)
        if not self.times:
            self.plot.setXRange(0, seconds, padding=0)
        self.update_plot()

    def serial_format(self):
        return f"{self.data_bits.currentData()}{self.parity.currentData()}{self.stop_bits.currentData():g}"

    def set_filter_size(self, channel, size):
        if channel not in range(CHANNEL_COUNT) or size not in (256, 128, 64, 32):
            return
        filter_ = self.channel_filters[channel]
        if filter_.size == size:
            return
        filter_.size = size
        filter_.reset()
        estimates = self.filtered_values[channel]
        estimates.clear()
        for elapsed, code in zip(self.times, self.values[channel]):
            value = filter_.feed(code, self.session_start + elapsed)
            estimates.append(float("nan") if value is None else value)
        self.filter_size_groups[channel].button(size).setChecked(True)
        if self.log_file is not None:
            self.record("CONFIG", b"", time.monotonic())
        self.update_plot()

    def reset_protocol(self, *_):
        if not hasattr(self, "hex_commands") or not hasattr(self, "crc_mode"):
            return
        working = self.receive_mode.currentData() == "fw018"
        lengths = FW018_LENGTHS if working else {command_code(number, self.hex_commands.isChecked()): length
                                               for number, length in [(3, 1), (5, 2), (25, 2)]}
        kwargs = dict(error_size=1, checksum_modes=("hex",)) if working else {}
        self.reply_parser = ReplyParser(lengths, **kwargs)
        self.log_reply_parser = ReplyParser(lengths, **kwargs)
        self.adc_parser = AdcStreamParser()
        self.log_adc_parser = AdcStreamParser()
        if self.log_file is not None:
            self.record("CONFIG", b"", time.monotonic())

    def change_receive_mode(self, *_):
        if self.threshold_worker is not None:
            self.read_thresholds.setChecked(False)
        working = self.receive_mode.currentData() == "fw018"
        self.hex_commands.setVisible(self.receive_mode.currentData() == "protocol")
        self.crc_mode.setVisible(self.receive_mode.currentData() == "protocol")
        self.gain_group.setVisible(working)
        self.baud.setCurrentText("9600" if self.receive_mode.currentData() == "protocol" else "19200")
        if working:
            self.hex_commands.setChecked(False)
            self.crc_mode.setCurrentIndex(self.crc_mode.findData("hex"))
        elif self.receive_mode.currentData() == "protocol":
            self.crc_mode.setCurrentIndex(self.crc_mode.findData("raw"))
        self.poll.setText("Опрос сигнала и порога · 200 мс" if working else "Опрос уровня каждые 200 мс")
        for button in self.query_buttons:
            if button.property("command") == 25:
                button.setText("Фон · 25" if working else "Уровень · 25")
        self.reset_protocol()
        self.gain_read_needed = False
        self.gain_requested = None
        self.queued_gain = None
        self.queued_query = None
        self.gain_button_group.setExclusive(False)
        for button in self.gain_buttons:
            button.setChecked(False)
        self.gain_button_group.setExclusive(True)
        self.gain_info.setText("Текущая ступень: —" if working else "В тестовой прошивке UART-команда недоступна")
        self.set_request_controls()

    def configure_channels(self, stream=False, channel_count=None):
        self.threshold_plot_visible = False
        self.threshold_legend_state = None
        self.plot.plotItem.legend.clear()
        working = self.buffer_format == "fw018" and self.source == "device"
        self.curves[0].setPen(pg.mkPen(CHANNEL_COLORS[1] if working else CHANNEL_COLORS[0], width=1.5))
        short_names = ("КЧЭ", "Тревога", "Батарея")
        count = channel_count if channel_count is not None else (3 if stream else 1)
        if not 1 <= count <= CHANNEL_COUNT:
            raise ValueError("Channel count must be between 1 and 8")
        for index, (check, curve) in enumerate(zip(self.channel_checks, self.curves)):
            enabled = index < count
            label = f"CH{index + 1}" + (f" · {short_names[index]}" if stream and enabled and index < 3 else "")
            if working and index == 0:
                label = "Сигнал · обработка"
            check.setText(label)
            check.setEnabled(enabled)
            check.setChecked(enabled)
            check.setToolTip(f"A{index}: {ADC_CHANNEL_NAMES[index]} · код АЦП" if stream and enabled and index < 3 else
                             "Текущий уровень прибора" if index == 0 else "Резерв")
            if enabled:
                legend_label = f"CH{index + 1} / A{index} · {ADC_CHANNEL_NAMES[index]}" if stream and index < 3 else label
                self.plot.plotItem.legend.addItem(curve, legend_label)
        self.channel_note.setText("\n".join(f"CH{index + 1} / A{index}: {name}"
                                            for index, name in enumerate(ADC_CHANNEL_NAMES)) if stream else
                                  "CH1: уровень, код / мВ\nCH2–CH8: резерв")
        if working:
            self.channel_note.setText("v0.18: обработанный сигнал и порог по UART.\nСырые CH1–CH3 и батарея не передаются.")
        self.plot.setLabel("left", "АЦП, код" if stream else "Уровень CH1, код")
        if working:
            self.plot.setLabel("left", "Обработка, ед.")
        self.ch2_readout.setVisible(stream)
        self.battery_readout.setVisible(stream)
        for channel, check in enumerate(self.filter_checks):
            enabled = self.channel_checks[channel].isEnabled()
            check.setEnabled(enabled)
            self.filter_size_widgets[channel].setEnabled(enabled)
        for frame in self.stat_frames:
            frame.setFixedHeight(48 if stream else 36)
        for label in self.stat_ch2_labels:
            label.setVisible(stream)
        for label in self.stat_ch3_labels:
            label.setVisible(stream)
        self.show_device_state(None, "тестовая прошивка не передаёт состояние кабеля" if stream else
                               "ожидание состояния прибора" if self.source == "device" else "демонстрационный режим")
        self.refresh_legend()
        # Recalculate after Qt has laid out the window and the legend labels.
        QtCore.QTimer.singleShot(0, self.refresh_legend)

    def refresh_legend(self):
        legend = self.plot.plotItem.legend
        legend.layout.activate()
        legend.updateSize()
        legend.setVisible(bool(legend.items))
        legend.update()

    def show_device_state(self, state, reason="нет данных"):
        self.last_state_stamp = time.monotonic() if state is not None else 0
        color = "#91a3bb"
        if state is None:
            text = "Диагностика кабеля: " + reason
        else:
            text = f"Состояние 0x{state:02X}: {describe_device_state(state)}"
            if state & 0x24:
                cable = []
                if state & 4:
                    cable.append("ОБРЫВ КАБЕЛЯ")
                if state & 32:
                    cable.append("КЗ КАБЕЛЯ")
                text = " / ".join(cable) + " · " + text
                color = "#ff927b"
            elif state == 0:
                text = "Кабель: обрыв / КЗ не обнаружены · " + text
                color = "#45d7d0"
            else:
                text = "Кабель: обрыв / КЗ не заявлены · " + text
                color = "#f4bd61"
        self.device_state_info.setText(text)
        self.device_state_info.setStyleSheet(f"color:{color}; font-weight:600")

    def set_request_controls(self):
        idle = self.pending is None
        mode = self.receive_mode.currentData()
        protocol = mode in ("protocol", "fw018")
        for button in self.query_buttons:
            button.setEnabled(self.device_ready and protocol
                              and (button.property("command") != 11 or mode == "fw018"))
        self.hex_commands.setEnabled(idle and mode == "protocol")
        self.crc_mode.setEnabled(idle and mode == "protocol")
        self.poll.setEnabled(self.device_ready and protocol)
        gain_enabled = self.device_ready and mode == "fw018"
        for widget in (*self.gain_buttons, self.gain_read):
            widget.setEnabled(gain_enabled)

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
        directory = application_directory() / "captures"
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
        self.buffer_format = self.receive_mode.currentData()
        self.configure_channels(self.buffer_format == "adc_stream")
        self.reset_protocol()
        self.log_path.setText(str(capture_path))
        self.badge.setText("●  ПРИБОР · A0–A2" if self.buffer_format == "adc_stream" else "●  ПРИБОР · УРОВЕНЬ CH1")
        if self.buffer_format == "fw018":
            self.badge.setText("●  ПРИБОР · v0.18 · UART")
        self.plot.enableAutoRange(axis="y", enable=True)
        self.worker = SerialWorker(port, int(self.baud.currentText()), self,
                                   bytesize=self.data_bits.currentData(), parity=self.parity.currentData(),
                                   stopbits=self.stop_bits.currentData())
        self.worker.opened.connect(self.device_opened)
        self.worker.received.connect(self.receive_data)
        self.worker.transmitted.connect(self.data_transmitted)
        self.worker.failed.connect(self.device_failed)
        self.worker.finished.connect(self.device_finished)
        self.port.setEnabled(False)
        self.baud.setEnabled(False)
        for widget in (self.data_bits, self.parity, self.stop_bits):
            widget.setEnabled(False)
        self.receive_mode.setEnabled(False)
        self.start.setEnabled(False)
        self.connect_button.setText("Отключить прибор")
        self.device_info.setText(f"Открытие {port} · {self.baud.currentText()} {self.serial_format()}…")
        self.worker.start()

    def device_opened(self):
        if self.worker is None or self.worker.isInterruptionRequested():
            return
        self.device_ready = True
        self.pending = None
        self.last_poll = time.monotonic()
        self.set_request_controls()
        stream = self.receive_mode.currentData() == "adc_stream"
        self.device_info.setText("Ожидается поток A0–A2 · без CRC · запросы не нужны." if stream else
                                 "Порт открыт. Нажмите «Версия ПО» для первого запроса. Обмен записывается в журнал.")
        self.sidebar_tabs.setCurrentIndex(2 if stream else 1)
        if stream and self.swd_auto_start:
            self.read_thresholds.setChecked(True)
        if self.receive_mode.currentData() == "fw018":
            self.gain_read_needed = True
            self.gain_requested = None
            self.gain_info.setText("Текущая ступень: чтение…")
            self.poll.setChecked(True)
            self.query(5)
        self.update_status()

    def device_failed(self, message):
        self.show_device_state(None, "ошибка COM-порта")
        self.queued_gain = None
        self.queued_query = None
        self.device_ready = False
        self.poll.setChecked(False)
        self.device_info.setText("Ошибка COM-порта: " + message)
        self.log_view.appendPlainText("ОШИБКА ПОРТА: " + message)
        self.set_request_controls()

    def device_finished(self):
        self.show_device_state(None, "прибор отключён")
        self.queued_gain = None
        self.queued_query = None
        worker = self.worker
        self.worker = None
        self.device_ready = False
        self.pending = None
        self.poll.setChecked(False)
        self.set_request_controls()
        self.port.setEnabled(True)
        self.baud.setEnabled(True)
        for widget in (self.data_bits, self.parity, self.stop_bits):
            widget.setEnabled(True)
        self.receive_mode.setEnabled(True)
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
            firmware = "fw018" if self.receive_mode.currentData() == "fw018" and not simulated else "legacy"
            if direction == "TX":
                description = describe_request(data, hex_numbers, firmware)
            elif not simulated and self.receive_mode.currentData() == "adc_stream":
                samples = self.log_adc_parser.feed(data)
                description = " | ".join(describe_adc_sample(sample) for sample in samples)
                if not description:
                    description = ("Фрагмент строки A0–A2; ожидается продолжение" if self.log_adc_parser.buffer else
                                   "Строка A0–A2 не распознана; проверьте скорость (19200 8N1) и формат")
            else:
                parser = (ReplyParser({command_code(number, hex_numbers): length
                                       for number, length in [(3, 1), (5, 2), (25, 2)]})
                          if simulated else self.log_reply_parser)
                replies = parser.feed(data)
                description = " | ".join(describe_reply(reply, hex_numbers, firmware) for reply in replies)
                if not description:
                    description = "Фрагмент ответа; ожидается продолжение" if parser.buffer else "Формат ответа или CRC не распознаны"
            source = "[ДЕМО]" if simulated else "[ПРИБОР]"
            hex_lines = [data[offset:offset + 16].hex(" ").upper() for offset in range(0, len(data), 16)] or [""]
            for index, hex_text in enumerate(hex_lines):
                details = description if index == 0 else "Продолжение байтов той же порции"
                self.log_view.appendPlainText(f"{utc} {source:<8} {direction}  {hex_text:<47}  | {details}")
        if self.log_file is not None and not simulated:
            record = dict(utc=utc, monotonic_s=stamp, direction=direction, hex=data.hex(" ").upper(),
                          command_numbers="HEX" if self.hex_commands.isChecked() else "DEC",
                          tx_crc=self.crc_mode.currentData(), port=self.port.currentData(),
                          receive_format=self.receive_mode.currentData(),
                          battery_vref_V=BATTERY_VREF_V, battery_r_top_kohm=BATTERY_R_TOP_KOHM,
                          battery_r_bottom_kohm=BATTERY_R_BOTTOM_KOHM,
                          baud=int(self.baud.currentText()), serial_format=self.serial_format(),
                          data_bits=self.data_bits.currentData(), parity=self.parity.currentData(),
                          stop_bits=self.stop_bits.currentData(), description=description)
            record.update(ch2_filter_size=self.alarm_filter.size, ch3_filter_size=self.battery_filter.size,
                          ch2_filter_enabled=self.alarm_filter_check.isChecked(),
                          ch3_filter_enabled=self.battery_filter_check.isChecked())
            record["channel_filters"] = [dict(channel=index + 1, size=filter_.size,
                                               enabled=check.isChecked())
                                         for index, (filter_, check) in enumerate(zip(self.channel_filters, self.filter_checks))]
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
        working = self.receive_mode.currentData() == "fw018"
        if (self.receive_mode.currentData() not in ("protocol", "fw018") or not self.device_ready
                or number not in ((3, 5, 11, 15, 25) if working else (3, 5, 25))):
            return
        if self.pending is not None:
            self.queued_query = number
            return
        code = number if working else command_code(number, self.hex_commands.isChecked())
        self.pending = dict(number=number, code=code, deadline=time.monotonic() + 1.0)
        self.last_poll = time.monotonic()
        self.set_request_controls()
        self.worker.send(encode_request(code, "hex" if working else self.crc_mode.currentData()))
        self.device_info.setText(f"Ожидается ответ: команда {number}, байт 0x{code:02X}…")

    def select_gain(self, step):
        if self.receive_mode.currentData() != "fw018" or not self.device_ready or step not in range(1, 9):
            return
        self.queued_gain = step
        self.gain_info.setText(f"Выбрана ступень {step} · ожидает отправки")
        self.check_requests()

    def apply_gain(self, step):
        if self.receive_mode.currentData() != "fw018" or not self.device_ready or self.pending is not None:
            return
        if step not in range(1, 9):
            return
        self.gain_requested = step
        self.gain_read_needed = False
        self.pending = dict(number=45, code=45, deadline=time.monotonic() + 1.0)
        self.last_poll = time.monotonic()
        self.set_request_controls()
        self.gain_info.setText(f"Запись ступени {step}…")
        self.worker.send(encode_request(45, "hex", bytes([step])))

    def data_transmitted(self, data, stamp):
        self.record("TX", data, stamp)
        if self.pending is not None:
            self.pending["deadline"] = stamp + 0.5
            if self.pending["code"] == 45:
                self.pending["confirm_at"] = stamp + 0.2

    def check_requests(self):
        now = time.monotonic()
        if self.last_state_stamp and now - self.last_state_stamp > 2:
            self.show_device_state(None, "нет свежего состояния более 2 с")
        # Confirm the actual setting even if the write acknowledgement was lost.
        if self.pending is not None and self.pending["code"] == 45 and now >= self.pending.get("confirm_at", float("inf")):
            self.pending = None
            self.gain_read_needed = True
        if self.pending is not None and now >= self.pending["deadline"]:
            code = self.pending["code"]
            self.pending = None
            self.poll.setChecked(False)
            self.show_device_state(None, "нет ответа от прибора")
            self.set_request_controls()
            self.gain_read_needed = False
            if self.queued_gain is not None:
                self.gain_info.setText("Смена усиления отменена: нет ответа от прибора")
            self.queued_gain = None
            self.queued_query = None
            if code in (15, 45):
                self.gain_info.setText("Усиление не подтверждено: нет ответа. Нажмите «Прочитать усиление».")
            detail = ("Проверьте прошивку v0.18, скорость и журнал."
                      if self.receive_mode.currentData() == "fw018" else "Проверьте журнал и вариант CRC / DEC / HEX.")
            message = f"Нет корректного ответа на 0x{code:02X} за 500 мс. " + detail
            if self.buffer_format == "fw018":
                self.last_uart_snapshot = None
                self.threshold_snapshot = None
                self.refresh_threshold_plot()
                self.threshold_info.setText("Пороги UART: опрос остановлен — нет корректного ответа")
            self.device_info.setText(message)
            self.log_view.appendPlainText(message)
        if self.receive_mode.currentData() == "fw018" and self.device_ready and self.pending is None and self.gain_read_needed:
            self.gain_read_needed = False
            self.query(15)
            return
        if self.device_ready and self.pending is None:
            if self.queued_gain is not None:
                step = self.queued_gain
                self.queued_gain = None
                self.apply_gain(step)
                return
            if self.queued_query is not None:
                number = self.queued_query
                self.queued_query = None
                self.query(number)
                return
        if (self.receive_mode.currentData() in ("protocol", "fw018") and self.device_ready and self.poll.isChecked()
                and self.pending is None and now - self.last_poll >= 0.2):
            self.query(11 if self.receive_mode.currentData() == "fw018" else 25)

    def receive_data(self, data, stamp):
        self.record("RX", data, stamp)
        if self.receive_mode.currentData() == "adc_stream":
            previous = self.adc_parser.rejected
            samples = self.adc_parser.feed(data)
            for sample in samples:
                self.add_device_sample(sample, stamp)
            if samples:
                self.device_info.setText(describe_adc_sample(samples[-1]))
            elif self.adc_parser.rejected != previous:
                self.device_info.setText("Поток A0–A2 не распознан. Проверьте скорость 19200, 8N1 и формат прошивки.")
            return
        previous = self.reply_parser.rejected
        for reply in self.reply_parser.feed(data):
            if self.pending is not None and reply.command == self.pending["code"]:
                self.pending = None
                self.set_request_controls()
            if reply.device_error:
                if reply.command in (3, 11):
                    self.show_device_state(None, "прибор отклонил чтение состояния")
                self.queued_gain = None
                self.queued_query = None
                self.poll.setChecked(False)
                if reply.command in (15, 45):
                    self.gain_read_needed = False
                    self.gain_info.setText(f"Команда усиления отклонена: ошибка {reply.data[0] if reply.data else '—'}")
                self.device_info.setText(f"Прибор отклонил команду 0x{reply.command:02X}; CRC {reply.checksum_mode} верна.")
                continue
            if self.receive_mode.currentData() == "fw018" and reply.command == 45:
                self.gain_read_needed = True
                self.gain_info.setText("Запись принята; контрольное чтение…")
                continue
            if self.receive_mode.currentData() == "fw018" and reply.command == 15:
                step = reply.data[0]
                suffix = ""
                if self.gain_requested is not None:
                    suffix = " · подтверждено" if step == self.gain_requested else f" · НЕ совпало с заданной {self.gain_requested}"
                self.gain_info.setText(f"Текущая ступень: {step}" + suffix + (" · вне диапазона 1–8" if step not in range(1, 9) else ""))
                self.gain_requested = None
                if self.queued_gain is not None:
                    self.gain_info.setText(self.gain_info.text() + f" · ожидает ступень {self.queued_gain}")
                elif step in range(1, 9):
                    self.gain_buttons[step - 1].setChecked(True)
                continue
            if self.receive_mode.currentData() == "fw018" and reply.command == 11:
                signal, trigger, state = decode_working_snapshot(reply.data)
                self.show_device_state(state)
                self.add_working_sample(signal, trigger, state, stamp)
                snapshot = ThresholdSnapshot(signal, self.last_noise, trigger, None, "UART")
                self.last_uart_snapshot = snapshot
                self.show_thresholds(snapshot)
                self.device_info.setText(f"v0.18 · сигнал {signal} · состояние 0x{state:02X} · CRC HEX: OK")
                continue
            if self.receive_mode.currentData() == "fw018" and reply.command == 25:
                self.last_noise = int.from_bytes(reply.data, "little")
                self.device_info.setText(f"v0.18 · оценка фона: {self.last_noise} ед. алгоритма · CRC HEX: OK")
                continue
            if reply.command == command_code(5, self.hex_commands.isChecked()):
                text = f"Версия ПО: {reply.data[0]}.{reply.data[1]}"
            elif reply.command == command_code(3, self.hex_commands.isChecked()):
                state = reply.data[0]
                self.show_device_state(state)
                text = f"Состояние: 0x{state:02X} · " + describe_device_state(state)
            else:
                value = int.from_bytes(reply.data, "little")
                self.add_device_sample(value, stamp)
                text = f"Уровень: {value} код · {value * 0.6105:.3f} мВ"
            self.device_info.setText(text + f" · CRC {reply.checksum_mode}: OK")
        if self.reply_parser.rejected != previous:
            self.log_view.appendPlainText("Ответ не распознан или CRC не совпала. Исходные байты сохранены.")

    def add_working_sample(self, signal, trigger, state, stamp):
        self.times.append(max(0.0, stamp - self.session_start))
        self.values[0].append(signal)
        self.feed_filters((signal,), stamp)
        self.working_thresholds.append(trigger)
        self.working_states.append(state)
        while self.times and self.times[0] < self.times[-1] - 60:
            for buffer in (self.times, self.values[0], self.filtered_values[0], self.working_thresholds, self.working_states):
                buffer.popleft()
        self.buffer_label.setText(f"v0.18 · {len(self.times)} измерений · обработанный сигнал UART")
        if not self.paused:
            self.update_plot()

    def add_device_sample(self, value, stamp):
        self.times.append(max(0.0, stamp - self.session_start))
        sample = value if isinstance(value, (tuple, list)) else (value,)
        for buffer, code in zip(self.values, sample):
            buffer.append(code)
        self.feed_filters(sample, stamp)
        while self.times and self.times[0] < self.times[-1] - 60:
            self.times.popleft()
            for buffer in self.values[:len(sample)]:
                buffer.popleft()
            for buffer in self.filtered_values[:len(sample)]:
                buffer.popleft()
        kind = f"{len(sample)} каналов" if len(sample) > 1 else "уровень по команде 25"
        self.buffer_label.setText(f"Прибор · {len(self.times)} измерений · {kind}")
        if not self.paused:
            self.update_plot()

    def feed_filters(self, sample, stamp):
        for filter_, buffer, code in zip(self.channel_filters, self.filtered_values, sample):
            estimate = filter_.feed(code, stamp)
            buffer.append(float("nan") if estimate is None else estimate)

    def toggle_thresholds(self, checked):
        if not checked:
            if self.threshold_worker is not None:
                self.threshold_worker.stop()
                self.read_thresholds.setEnabled(False)
            self.threshold_info.setText("Пороги: чтение через ST-Link выключено")
            self.threshold_snapshot = None
            self.last_swd_snapshot = None
            if self.buffer_format == "fw018" and self.last_uart_snapshot is not None:
                self.show_thresholds(self.last_uart_snapshot)
            self.refresh_threshold_plot()
            return
        self.threshold_info.setText("Пороги: подключение к ST-Link через SWD…")
        self.threshold_reads = 0
        self.threshold_worker = ThresholdWorker(self, firmware=self.receive_mode.currentData())
        self.threshold_worker.updated.connect(self.show_thresholds)
        self.threshold_worker.failed.connect(self.thresholds_failed)
        self.threshold_worker.finished.connect(self.thresholds_finished)
        self.threshold_worker.start()

    def show_thresholds(self, snapshot):
        working = self.buffer_format == "fw018"
        if snapshot.source == "SWD":
            self.last_swd_snapshot = snapshot
            self.last_swd_stamp = time.monotonic()
        elif (working and self.read_thresholds.isChecked() and self.last_swd_snapshot is not None
              and time.monotonic() - self.last_swd_stamp < 3):
            snapshot = ThresholdSnapshot(snapshot.signal, self.last_swd_snapshot.noise,
                                         self.last_swd_snapshot.trigger, self.last_swd_snapshot.release, "UART + SWD")
        if snapshot.source.startswith("UART") or self.read_thresholds.isChecked():
            self.threshold_reads += 1
            updated = datetime.now().astimezone().strftime("%H:%M:%S")
            release = f"±{snapshot.release:.4f}" if snapshot.release is not None else "не передаётся UART"
            noise = f"{snapshot.noise:.4f}" if snapshot.noise is not None else "—"
            self.threshold_info.setText(
                f"Пороги {snapshot.source} · срабатывание: ±{snapshot.trigger:.4f} · возврат: {release} · ед. алгоритма\n"
                f"Обновлено {updated} · чтение №{self.threshold_reads} · "
                f"сигнал: {snapshot.signal:.2f} · фон: {noise}")
            self.threshold_snapshot = snapshot
            self.threshold_times.append(max(0.0, time.monotonic() - self.session_start))
            self.threshold_signals.append(snapshot.signal)
            while self.threshold_times and self.threshold_times[0] < self.threshold_times[-1] - 60:
                self.threshold_times.popleft()
                self.threshold_signals.popleft()
            self.refresh_threshold_plot()

    def thresholds_failed(self, message):
        self.last_swd_snapshot = None
        if self.buffer_format == "fw018" and self.last_uart_snapshot is not None:
            self.show_thresholds(self.last_uart_snapshot)
            return
        self.threshold_snapshot = None
        self.threshold_times.clear()
        self.threshold_signals.clear()
        self.refresh_threshold_plot()
        self.threshold_info.setText("Пороги недоступны: " + message + " Повторное чтение автоматически.")

    def sync_threshold_view(self):
        self.threshold_view.setGeometry(self.plot.getViewBox().sceneBoundingRect())
        self.threshold_view.linkedViewChanged(self.plot.getViewBox(), self.threshold_view.XAxis)

    def refresh_threshold_plot(self):
        working = self.buffer_format == "fw018"
        snapshot = self.threshold_snapshot
        visible = (snapshot is not None and (snapshot.source.startswith("UART") or self.read_thresholds.isChecked())
                   and self.source == "device" and self.buffer_format in ("adc_stream", "fw018"))
        if visible and not working:
            self.threshold_axis.show()
        else:
            self.threshold_axis.hide()
        self.threshold_view.setVisible(visible)
        self.threshold_signal_curve.setVisible(not working)
        items = [self.threshold_signal_curve, *self.threshold_legend_samples]
        legend_state = (visible, working, snapshot.source if visible else None,
                        snapshot.release is not None if visible else False)
        if legend_state != self.threshold_legend_state:
            legend = self.plot.plotItem.legend
            names = ("Обработанный CH2 · SWD", "± Срабатывание", "± Возврат")
            for index, (item, name) in enumerate(zip(items, names)):
                legend.removeItem(item)
                if visible and not (index == 0 and working) and not (index == 2 and snapshot.release is None):
                    legend.addItem(item, name)
            self.threshold_plot_visible = visible
            self.threshold_legend_state = legend_state
            self.refresh_legend()
            QtCore.QTimer.singleShot(0, self.refresh_legend)
        if not visible or self.paused:
            return
        for sign, trigger, release in zip((1, -1), self.trigger_lines, self.release_lines):
            trigger.setPos(sign * snapshot.trigger)
            release.setVisible(snapshot.release is not None)
            if snapshot.release is not None:
                release.setPos(sign * snapshot.release)
        self.threshold_signal_curve.setData(list(self.threshold_times), list(self.threshold_signals))
        extent = max(1, snapshot.trigger, *(abs(value) for value in self.threshold_signals)) * 1.2
        self.threshold_view.setYLink(self.plot.getViewBox() if working else None)
        if working:
            self.plot.setYRange(-extent, extent, padding=0)
        self.threshold_view.setYRange(-extent, extent, padding=0)
        self.sync_threshold_view()

    def thresholds_finished(self):
        worker = self.threshold_worker
        self.threshold_worker = None
        self.read_thresholds.setEnabled(True)
        if worker is not None:
            worker.deleteLater()

    def closeEvent(self, event):
        if self.threshold_worker is not None:
            self.threshold_worker.stop()
            if not self.threshold_worker.wait(6000):
                self.threshold_info.setText("Ожидается завершение чтения ST-Link. Повторите закрытие.")
                event.ignore()
                return
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
            self.buffer_format = "protocol"
            self.configure_channels()
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
        for index, (buffer, signal) in enumerate(zip(self.values, signals)):
            codes = np.clip(np.rint(signal), 0, 4095).astype(int)
            buffer.extend(codes)
            for code, elapsed in zip(codes, times):
                estimate = self.channel_filters[index].feed(code, self.session_start + elapsed)
                self.filtered_values[index].append(float("nan") if estimate is None else estimate)
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
        self.plot.showAxis("right", self.buffer_format == "adc_stream" and self.channel_checks[1].isChecked())
        if self.buffer_format == "adc_stream" and self.channel_checks[2].isChecked():
            self.battery_axis.show()
        else:
            self.battery_axis.hide()
        self.refresh_threshold_plot()
        if not self.times or self.paused:
            return
        times = np.asarray(self.times)
        seconds = self.window_seconds
        right = max(seconds, times[-1])
        left = right - seconds
        start = np.searchsorted(times, left)
        filtered = self.buffer_format == "adc_stream" and self.battery_filter_check.isChecked()
        alarm_filtered = self.buffer_format == "adc_stream" and self.alarm_filter_check.isChecked()
        buffers = list(self.values)
        for index, check in enumerate(self.filter_checks):
            if check.isChecked() and check.isEnabled():
                buffers[index] = self.filtered_values[index]
        for curve, buffer in zip(self.curves, buffers):
            if buffer:
                curve.setData(times[start:], np.asarray(buffer)[start:])
        self.plot.setXRange(left, right, padding=0)
        if self.buffer_format == "adc_stream" and self.values[1]:
            code = buffers[1][-1]
            if np.isfinite(code):
                number = f"{code:.1f}" if alarm_filtered else f"{code:.0f}"
                self.ch2_readout.setText(f"CH2: {number} код / {code * CH2_MV_PER_CODE:.1f} мВ"
                                         + (f" · фильтр {self.alarm_filter.size}" if alarm_filtered else " · без фильтра"))
            else:
                self.ch2_readout.setText(f"CH2: набор {len(self.alarm_filter.samples)}/{self.alarm_filter.size}")
        if self.buffer_format == "adc_stream" and self.values[2]:
            code = buffers[2][-1]
            if np.isfinite(code):
                suffix = f" · фильтр {self.battery_filter.size}" if filtered else " · без фильтра"
                self.battery_readout.setText(f"CH3: {code * BATTERY_V_PER_CODE:.3f} В" + suffix)
            else:
                self.battery_readout.setText(f"CH3: набор {len(self.battery_filter.samples)}/{self.battery_filter.size}")
        values = np.asarray(buffers[0], dtype=float)[start:]
        values = values[np.isfinite(values)]
        if len(values):
            precision = 1 if self.filter_checks[0].isChecked() else 0
            numbers = [f"{values[-1]:.{precision}f}", f"{values.min():.{precision}f}", f"{values.max():.{precision}f}",
                       f"{np.ptp(values):.{precision}f}", f"{np.std(values):.2f}"]
            for label, number in zip(self.stat_labels, numbers):
                label.setText(("Сигнал " if self.buffer_format == "fw018" else "CH1 ") + number)
        else:
            for label in self.stat_labels:
                label.setText("Сигнал —" if self.buffer_format == "fw018" else "CH1 —")
        for label in self.stat_labels:
            label.setToolTip(("Сигнал: " if self.buffer_format == "fw018" else "CH1: ")
                             + (f"с фильтром, массив {self.channel_filters[0].size}" if self.filter_checks[0].isChecked()
                                else "исходные отсчёты") + "; статистика по видимому окну")
        if self.buffer_format == "adc_stream" and self.values[1]:
            values = np.asarray(buffers[1], dtype=float)[start:]
            values = values[np.isfinite(values)]
            if len(values):
                precision = 1 if alarm_filtered else 0
                numbers = [f"{values[-1]:.{precision}f}", f"{values.min():.{precision}f}", f"{values.max():.{precision}f}",
                           f"{np.ptp(values):.{precision}f}", f"{np.std(values):.2f}"]
                for label, number in zip(self.stat_ch2_labels, numbers):
                    label.setText("CH2 " + number)
            else:
                for label in self.stat_ch2_labels:
                    label.setText("CH2 —")
            for label in self.stat_ch2_labels:
                label.setToolTip("CH2: " + (f"с фильтром, массив {self.alarm_filter.size}" if alarm_filtered else "исходные отсчёты")
                                 + "; статистика по видимому окну, коды АЦП")
        if self.buffer_format == "adc_stream" and self.values[2]:
            voltage = np.asarray(buffers[2], dtype=float)[start:] * BATTERY_V_PER_CODE
            voltage = voltage[np.isfinite(voltage)]
            if len(voltage):
                numbers = [voltage[-1], voltage.min(), voltage.max(), np.ptp(voltage), np.std(voltage)]
                for label, number in zip(self.stat_ch3_labels, numbers):
                    label.setText(f"CH3 {number:.3f} В")
            else:
                for label in self.stat_ch3_labels:
                    label.setText("CH3 — В")
            for label in self.stat_ch3_labels:
                label.setToolTip("CH3: " + (f"с фильтром, массив {self.battery_filter.size}" if filtered else "исходные отсчёты")
                                 + "; статистика по видимому окну, В")

    def autoscale(self):
        self.plot.enableAutoRange(axis="y", enable=True)
        self.update_plot()

    def clear_data(self):
        self.show_device_state(None, "ожидание состояния прибора")
        self.working_thresholds.clear()
        self.working_states.clear()
        self.last_uart_snapshot = None
        self.last_swd_snapshot = None
        self.last_noise = None
        for filter_, buffer in zip(self.channel_filters, self.filtered_values):
            filter_.reset()
            buffer.clear()
        self.threshold_times.clear()
        self.threshold_signals.clear()
        self.threshold_snapshot = None
        self.threshold_signal_curve.setData([], [])
        self.refresh_threshold_plot()
        self.times.clear()
        for buffer, curve in zip(self.values, self.curves):
            buffer.clear()
            curve.setData([], [])
        self.sample_index = 0
        self.demo_logged_sample = 0
        self.session_start = time.monotonic()
        for label in self.stat_labels:
            label.setText("CH1 —")
        for label in self.stat_ch2_labels:
            label.setText("CH2 —")
        for label in self.stat_ch3_labels:
            label.setText("CH3 — В")
        self.buffer_label.setText("Буфер пуст")
        self.ch2_readout.setText("CH2: — код / — мВ")
        self.battery_readout.setText("CH3: — В")
        self.plot.setXRange(0, self.window_seconds, padding=0)

    def write_csv(self, path):
        def formatted(value, scale=1):
            return f"{value * scale:.6f}" if np.isfinite(value) else ""

        with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            if self.buffer_format == "fw018":
                writer.writerow(["source", "time_s", "processed_signal", "trigger_threshold", "device_state",
                                 f"processed_signal_trimmed{self.channel_filters[0].size}"])
                writer.writerows((self.source, f"{stamp:.6f}", signal, trigger, state, formatted(estimate))
                                 for stamp, signal, trigger, state, estimate in zip(self.times, self.values[0],
                                                                         self.working_thresholds, self.working_states,
                                                                         self.filtered_values[0]))
                return
            if self.buffer_format == "adc_stream":
                count = max((index + 1 for index, buffer in enumerate(self.values) if buffer), default=3)
                columns = [ADC_CSV_COLUMNS[index] if index < 3 else f"ch{index + 1}_code" for index in range(count)]
                header = ["source", "time_s", *columns]
                for channel in range(count):
                    prefix = f"a{channel}" if channel < 3 else f"ch{channel + 1}"
                    size = self.channel_filters[channel].size
                    header.append(f"{prefix}_trimmed{size}_code")
                    if channel in (1, 2):
                        header.append(f"alarm_trimmed{size}_mV" if channel == 1 else f"battery_trimmed{size}_V")
                writer.writerow(header)
                for row in zip(self.times, *self.values[:count], *self.filtered_values[:count]):
                    output = [self.source, f"{row[0]:.6f}", *row[1:count + 1]]
                    for channel, estimate in enumerate(row[count + 1:]):
                        output.append(f"{estimate:.4f}" if np.isfinite(estimate) else "")
                        if channel in (1, 2):
                            output.append(formatted(estimate, CH2_MV_PER_CODE if channel == 1 else BATTERY_V_PER_CODE))
                    writer.writerow(output)
                return
            size = self.channel_filters[0].size
            writer.writerow(["source", "time_s", "ch1_level_code", f"ch1_trimmed{size}_code",
                             f"ch1_trimmed{size}_mV", "ch1_level_mV"])
            writer.writerows((self.source, f"{stamp:.6f}", value, formatted(estimate), formatted(estimate, 0.6105),
                              f"{value * 0.6105:.4f}")
                             for stamp, value, estimate in zip(self.times, self.values[0], self.filtered_values[0]))

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
        QtWidgets.QMessageBox.about(self, "О программе", "ADC / DAC Testbench\nВерсия 0.5 — тестовая и рабочая v0.18\n\n"
                                  "Поток A0–A2: CH1–CH3, 19200 8N1, без CRC.\n"
                                  "v0.18: сигнал, порог и усиление по UART, 19200 8N1.\n"
                                  "Протокол 0.0: ручной выбор DEC/HEX и CRC.\n"
                                  "Статистика CH1 рассчитана по видимому временному окну.\nСКО — отклонение от среднего значения.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="Render demo and save preview.png")
    args = parser.parse_args()
    app = QtWidgets.QApplication(sys.argv[:1])
    if sys.platform == "win32":
        import os

        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ["segoeui.ttf", "segoeuib.ttf", "consola.ttf"]:
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
