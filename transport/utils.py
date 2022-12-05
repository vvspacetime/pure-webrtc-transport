import os
from struct import unpack


def random16() -> int:
    return unpack("!H", os.urandom(2))[0]


def random32() -> int:
    return unpack("!L", os.urandom(4))[0]


def uint16_add(a: int, b: int) -> int:
    """
    Return a + b.
    """
    return (a + b) & 0xFFFF


def uint16_sub(a: int, b: int) -> int:
    """
    Return a - b.
    """
    c = a - b
    return c if c >= 0 else c + 0x10000


def uint16_gt(a: int, b: int) -> bool:
    """
    Return a > b.
    """
    half_mod = 0x8000
    return ((a < b) and ((b - a) > half_mod)) or ((a > b) and ((a - b) < half_mod))


def uint16_gte(a: int, b: int) -> bool:
    """
    Return a >= b.
    """
    return (a == b) or uint16_gt(a, b)


def uint32_add(a: int, b: int) -> int:
    """
    Return a + b.
    """
    return (a + b) & 0xFFFFFFFF


def uint32_sub(a: int, b: int) -> int:
    """
    Return a - b
    """
    c = a - b
    return c if c >= 0 else c + 0x100000000


def uint32_gt(a: int, b: int) -> bool:
    """
    Return a > b.
    """
    half_mod = 0x80000000
    return ((a < b) and ((b - a) > half_mod)) or ((a > b) and ((a - b) < half_mod))


def uint32_gte(a: int, b: int) -> bool:
    """
    Return a >= b.
    """
    return (a == b) or uint32_gt(a, b)


class Uint32Unwrapper:
    def __init__(self):
        self._last_unwrapped = None
        self._last_value = None

    def unwrap(self, value):
        if self._last_value:
            diff = uint32_sub(value, self._last_value)
            self._last_unwrapped = diff + self._last_value
            if not uint32_gte(value, self._last_value):
                self._last_unwrapped -= 0x100000000
        else:
            self._last_unwrapped = value
        self._last_value = value
        return self._last_unwrapped
