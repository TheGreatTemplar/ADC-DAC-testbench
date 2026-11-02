"""Desktop prototype for the ADC/DAC testbench. Device protocol is pending."""

import argparse
import csv
import sys
from collections import deque
from pathlib import Path

import numpy as np
try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from serial.tools import list_ports

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
        self.running = False
        self.paused = False
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
        self.statusBar().showMessage("Готов к работе • Источник: демонстрационный сигнал")

    def build_menu(self):
        file_menu = self.menuBar().addMenu("Файл")
        self.export_action = file_menu.addAction("Сохранить буфер в CSV…", self.export_csv)
        self.export_action.setShortcut("Ctrl+S")
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
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(18)
        layout.addLayout(body, 1)
        sidebar = QtWidgets.QWidget()
        sidebar.setFixedWidth(260)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(14)
        body.addWidget(sidebar)
        connection = QtWidgets.QGroupBox("ПОДКЛЮЧЕНИЕ")
        form = QtWidgets.QFormLayout(connection)
        form.setSpacing(10)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumWidth(130)
        form.addRow("COM-порт", self.port)
        self.baud = QtWidgets.QComboBox()
        self.baud.addItems(["9600", "57600", "115200", "230400", "460800", "921600"])
        self.baud.setCurrentText("115200")
        form.addRow("Скорость", self.baud)
        format_label = QtWidgets.QLabel("8 бит · без чётности · 1 стоп-бит")
        format_label.setObjectName("muted")
        form.addRow(format_label)
        refresh = QtWidgets.QPushButton("Обновить список портов")
        refresh.clicked.connect(self.refresh_ports)
        form.addRow(refresh)
        connect = QtWidgets.QPushButton("Подключить прибор")
        connect.setEnabled(False)
        connect.setToolTip("Будет доступно после определения протокола прибора")
        form.addRow(connect)
        note = QtWidgets.QLabel("Протокол прибора пока не задан.\nСейчас доступен демонстрационный сигнал.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)
        side.addWidget(connection)
        channels = QtWidgets.QGroupBox("КАНАЛЫ АЦП")
        channel_layout = QtWidgets.QGridLayout(channels)
        self.channel_checks = []
        for index, color in enumerate(CHANNEL_COLORS):
            check = QtWidgets.QCheckBox(f"CH{index + 1}")
            check.setChecked(True)
            check.setStyleSheet(f"color:{color}; font-weight:600")
            check.toggled.connect(self.update_plot)
            self.channel_checks.append(check)
            channel_layout.addWidget(check, index // 2, index % 2)
        channel_layout.addWidget(QtWidgets.QLabel("Единицы: коды АЦП\nДемо: 12 бит, 0…4095"), 4, 0, 1, 2)
        side.addWidget(channels)
        acquisition = QtWidgets.QGroupBox("ОТОБРАЖЕНИЕ")
        acquisition_form = QtWidgets.QFormLayout(acquisition)
        self.window_seconds = QtWidgets.QComboBox()
        self.window_seconds.addItems(["2", "5", "10", "30", "60"])
        self.window_seconds.setCurrentText("10")
        self.window_seconds.currentTextChanged.connect(self.update_plot)
        acquisition_form.addRow("Окно, с", self.window_seconds)
        acquisition_form.addRow(QtWidgets.QLabel("Демо: 1 000 отсчётов/с на канал\nБуфер: последние 60 секунд"))
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
        self.plot.setLabel("left", "Код АЦП")
        self.plot.setLabel("bottom", "Время", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(15, 10))
        self.plot.setYRange(1950, 2370)
        self.plot.setXRange(0, 10, padding=0)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.curves = [self.plot.plot(pen=pg.mkPen(color, width=1.5), name=f"CH{index + 1}")
                       for index, color in enumerate(CHANNEL_COLORS)]
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

    def refresh_ports(self):
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
        self.running = not self.running
        if self.running:
            self.timer.start()
        else:
            self.timer.stop()
        self.start.setText("Остановить демо" if self.running else "Запустить демо")
        self.update_status()

    def update_status(self):
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
        signals = [2048 + index * 36 + (23 - index) * np.sin(2 * np.pi * (1.2 - index * 0.12) * times)
                   + self.rng.normal(0, 5, count) for index in range(CHANNEL_COUNT)]
        self.times.extend(times)
        for buffer, signal in zip(self.values, signals):
            buffer.extend(np.clip(np.rint(signal), 0, 4095).astype(int))
        self.buffer_label.setText(f"В буфере: {len(self.times):,} отсчётов/канал · демо · 1 000 отсчётов/с".replace(",", " "))
        if not self.paused:
            self.update_plot()

    def update_plot(self, *_):
        if not hasattr(self, "curves"):
            return
        for curve, check in zip(self.curves, self.channel_checks):
            curve.setVisible(check.isChecked())
        if not self.times or self.paused:
            return
        times = np.asarray(self.times)
        seconds = float(self.window_seconds.currentText())
        right = max(seconds, times[-1])
        left = right - seconds
        start = np.searchsorted(times, left)
        for curve, buffer in zip(self.curves, self.values):
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
        for label in self.stat_labels:
            label.setText("—")
        self.buffer_label.setText("Буфер пуст")
        self.plot.setXRange(0, float(self.window_seconds.currentText()), padding=0)

    def write_csv(self, path):
        with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["source", "time_s"] + [f"adc{index + 1}_code" for index in range(CHANNEL_COUNT)])
            writer.writerows(("demo", f"{row[0]:.6f}", *row[1:])
                             for row in zip(self.times, *self.values))

    def export_csv(self):
        if not self.times:
            QtWidgets.QMessageBox.information(self, "Сохранение", "Буфер пуст. Сначала запустите демо.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить буфер", "adc_demo.csv", "CSV (*.csv)")
        if path:
            try:
                self.write_csv(path)
                self.statusBar().showMessage(f"Сохранено: {path}")
            except OSError as error:
                QtWidgets.QMessageBox.warning(self, "Ошибка сохранения", str(error))

    def about(self):
        QtWidgets.QMessageBox.about(self, "О программе", "ADC / DAC Testbench\nПрототип интерфейса 0.2\n\n"
                                  "Восемь демонстрационных каналов АЦП.\nПодключение прибора и управление ЦАП — после определения протокола.\n"
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
