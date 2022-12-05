import asyncio
from asyncio import Queue
from collections import deque
from typing import Deque

from transport import RtpPacket


class Pacer:
    def __init__(self):
        self.input_queue: Deque[RtpPacket] = deque()
        self.output_queue = Queue()
        self.target_bitrate = 1000000  # 1m
        self.bytes_remaining = 0
        self.max_bytes_in_budget = 0
        self.update_bitrate(self.target_bitrate)
        asyncio.ensure_future(self.run_loop())

    def enqueue(self, rtp: RtpPacket):
        self.input_queue.append(rtp)

    async def read_queue(self) -> RtpPacket:
        return await self.output_queue.get()

    def update_bitrate(self, bitrate: int):
        self.target_bitrate = bitrate * 1.1
        self.max_bytes_in_budget = 0.5 * self.target_bitrate / 8
        self.bytes_remaining = min(max(-self.max_bytes_in_budget, self.bytes_remaining), self.max_bytes_in_budget)

    async def run_loop(self):
        while True:
            self.__add_budget((self.target_bitrate / 8) * 0.005)
            # print("{}, remain={}".format(clock.current_ms(), self.bytes_remaining))
            while self.bytes_remaining > 0 and len(self.input_queue) > 0:
                pkt = self.input_queue.popleft()
                await self.output_queue.put(pkt)
                self.__use_budget(len(pkt.payload))
            # print("{}, remain={}".format(clock.current_ms(), self.bytes_remaining))
            await asyncio.sleep(0.005)

    def __add_budget(self, data_bytes):
        if self.bytes_remaining < 0:
            self.bytes_remaining = min(self.bytes_remaining + data_bytes, self.max_bytes_in_budget)
        else:
            self.bytes_remaining = min(data_bytes, self.max_bytes_in_budget)  # no accumulate

    def __use_budget(self, data_bytes):
        self.bytes_remaining = max(self.bytes_remaining - data_bytes, -self.max_bytes_in_budget)
