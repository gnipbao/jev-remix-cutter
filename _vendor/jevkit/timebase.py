"""Half-open source microseconds and output frames; round nearest, ties upward."""
from fractions import Fraction

def round_half_up(value):
    value = Fraction(value)
    if value < 0:
        return -round_half_up(-value)
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)

def us_to_frame(time_us, fps):
    return round_half_up(Fraction(time_us) * Fraction(str(fps)) / 1_000_000)

def frame_to_us(frame, fps):
    return round_half_up(Fraction(frame) * 1_000_000 / Fraction(str(fps)))
