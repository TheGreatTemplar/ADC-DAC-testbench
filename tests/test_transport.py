import time
import unittest

from qt_compat import QtCore
from transport import SerialWorker


class TestTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def test_loopback_and_clean_stop(self):
        received = []
        sent = []
        opened = []
        errors = []
        worker = SerialWorker("loop://", 9600)
        worker.opened.connect(lambda: opened.append(True))
        worker.received.connect(lambda data, stamp: received.append(data))
        worker.transmitted.connect(lambda data, stamp: sent.append(data))
        worker.failed.connect(errors.append)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not opened and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertTrue(opened)
            worker.send(b"&05\x05\r")
            while len(b"".join(received)) < 5 and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertEqual(b"".join(sent), b"&05\x05\r")
            self.assertEqual(b"".join(received), b"&05\x05\r")
            self.assertFalse(errors)
        finally:
            worker.requestInterruption()
            self.assertTrue(worker.wait(1500))
