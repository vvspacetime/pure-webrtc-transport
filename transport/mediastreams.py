import asyncio
import fractions
import time
import uuid
from abc import ABCMeta, abstractmethod
from typing import Tuple

from pyee.asyncio import AsyncIOEventEmitter
from .rtp import RtpPacket

AUDIO_PTIME = 0.020  # 20ms audio packetization
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 1 / 30  # 30fps
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)


def convert_timebase(
    pts: int, from_base: fractions.Fraction, to_base: fractions.Fraction
) -> int:
    if from_base != to_base:
        pts = int(pts * from_base / to_base)
    return pts


class MediaStreamError(Exception):
    pass


class MediaStreamTrack(AsyncIOEventEmitter, metaclass=ABCMeta):
    """
    A single media track within a stream.
    """

    kind = "unknown"

    def __init__(self) -> None:
        super().__init__()
        self.__ended = False
        self._id = str(uuid.uuid4())

    @property
    def id(self) -> str:
        """
        An automatically generated globally unique ID.
        """
        return self._id

    @property
    def readyState(self) -> str:
        return "ended" if self.__ended else "live"

    @abstractmethod
    async def recv(self) -> RtpPacket:
        """
        Receive the next :class:`~av.audio.frame.AudioFrame` or :class:`~av.video.frame.VideoFrame`.
        """

    def stop(self) -> None:
        if not self.__ended:
            self.__ended = True
            self.emit("ended")

            # no more events will be emitted, so remove all event listeners
            # to facilitate garbage collection.
            self.remove_all_listeners()

