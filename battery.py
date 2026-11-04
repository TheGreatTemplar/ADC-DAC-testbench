"""Trimmed batch estimate for ADC channels; raw samples stay intact."""


class BatteryBatchFilter:
    def __init__(self, size=256):
        self.size = size
        self.reset()

    def reset(self):
        self.samples = []
        self.value = None
        self.last_stamp = None

    def feed(self, code, stamp):
        # A new acquisition must not reuse yesterday's partial batch or result.
        if self.last_stamp is not None and stamp - self.last_stamp > 2:
            self.reset()
        self.last_stamp = stamp
        self.samples.append(code)
        if len(self.samples) == self.size:
            ordered = sorted(self.samples)
            trim = self.size // 10
            central = ordered[trim:self.size - trim]
            self.value = sum(central) / len(central)
            self.samples.clear()
        return self.value
