"""Control the Phase 11 PL marker through its one-word AXI GPIO register."""


class PLVideoMarker:
    """Pack one projected marker into the PL video overlay configuration.

    Bit layout matches ``acoustic_video_marker.vhd``:
    enable[31], radius[25:20], y[19:10], x[9:0].
    """

    def __init__(self, gpio, radius_px=6):
        if type(radius_px) is not int or not 0 <= radius_px <= 63:
            raise ValueError('radius_px must be an integer in 0..63')
        self._channel = gpio.channel1
        self._radius = radius_px
        self._last_word = None
        self._channel.setdirection('out')
        self.set_pixel(None)

    def set_pixel(self, pixel):
        """Enable the point at ``(x, y)`` or disable it with ``None``."""
        if pixel is None:
            word = 0
        else:
            if (len(pixel) != 2 or any(type(value) is not int for value in pixel)
                    or any(value < 0 or value > 1023 for value in pixel)):
                raise ValueError('pixel must contain two integer coordinates in 0..1023')
            x, y = pixel
            word = (1 << 31) | (self._radius << 20) | (y << 10) | x
        if word != self._last_word:
            self._channel.write(word)
            self._last_word = word
        return word
