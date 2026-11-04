"""Serial I/O lives in a worker thread; Qt widgets stay on the GUI thread."""

import queue
import time

import serial
from qt_compat import QtCore


class SerialWorker(QtCore.QThread):
    opened = QtCore.Signal()
    received = QtCore.Signal(bytes, float)
    transmitted = QtCore.Signal(bytes, float)
    failed = QtCore.Signal(str)

    def __init__(self, port, baudrate, parent=None, *, bytesize=serial.EIGHTBITS,
                 parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE):
        super().__init__(parent)
        self.port_name = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.outgoing = queue.Queue()

    def send(self, data):
        self.outgoing.put(bytes(data))

    def run(self):
        connection = None
        try:
            connection = serial.serial_for_url(self.port_name, baudrate=self.baudrate,
                                               bytesize=self.bytesize, parity=self.parity,
                                               stopbits=self.stopbits, timeout=0.05,
                                               write_timeout=0.5, xonxoff=False, rtscts=False,
                                               dsrdtr=False, do_not_open=True)
            connection.dtr = False
            connection.rts = False
            connection.open()
            self.opened.emit()
            while not self.isInterruptionRequested():
                try:
                    data = self.outgoing.get_nowait()
                except queue.Empty:
                    data = None
                if data is not None:
                    if connection.write(data) != len(data):
                        raise serial.SerialException("Incomplete write")
                    self.transmitted.emit(data, time.monotonic())
                data = connection.read(min(max(connection.in_waiting, 1), 4096))
                if data:
                    self.received.emit(data, time.monotonic())
        except (serial.SerialException, OSError) as error:
            self.failed.emit(str(error))
        finally:
            if connection is not None:
                connection.close()
