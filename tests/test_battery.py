import unittest

from battery import BatteryBatchFilter


class TestBatteryFilter(unittest.TestCase):
    def test_all_selectable_batch_sizes_trim_outliers_and_hold_between_batches(self):
        for size in (256, 128, 64, 32):
            filter_ = BatteryBatchFilter(size)
            trim = size // 10
            values = [0] * trim + [2000] * (size - 2 * trim) + [4095] * trim
            for index, code in enumerate(values[:-1]):
                self.assertIsNone(filter_.feed(code, index / 57))
            self.assertEqual(filter_.feed(values[-1], (size - 1) / 57), 2000)
            self.assertEqual(filter_.feed(1000, size / 57), 2000)

    def test_batch_rejects_sparse_dips_and_spikes(self):
        filter_ = BatteryBatchFilter()
        codes = [3000] * 23 + [3700] * 210 + [4000] * 23
        for index, code in enumerate(codes[:-1]):
            self.assertIsNone(filter_.feed(code, index / 57))
        self.assertEqual(filter_.feed(codes[-1], 255 / 57), 3700)

    def test_real_level_drop_is_accepted_in_the_next_batch(self):
        filter_ = BatteryBatchFilter()
        for index in range(256):
            value = filter_.feed(3700, index / 57)
        self.assertEqual(value, 3700)
        for index in range(255):
            self.assertEqual(filter_.feed(3500, (256 + index) / 57), 3700)
        self.assertEqual(filter_.feed(3500, 511 / 57), 3500)

    def test_new_acquisition_does_not_reuse_a_stale_result(self):
        filter_ = BatteryBatchFilter()
        for index in range(256):
            filter_.feed(3700, index / 57)
        self.assertIsNone(filter_.feed(3500, 86400))
        self.assertEqual(filter_.samples, [3500])
