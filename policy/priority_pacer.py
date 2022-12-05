from asyncio import Queue
from sortedcontainers import SortedDict
from transport import RtpPacket, utils


class MediaPriority:
    RTX = 0
    AUDIO = 1
    VIDEO = 3
    OTHER = 4


def get_and_set_default(d, k, dv):
    if k in d:
        return d[k]
    else:
        d[k] = dv
        return d[k]


class PriorityPacer:
    def __init__(self):
        # video: { (layer, ts): [p1, p2] }, audio: { (0, ts), [p1, p3] }
        # using tag = (layer, ts)
        self.input_queue = SortedDict()
        self.output_queue = Queue()
        self.target_bitrate = 1000000  # 1m
        self.bytes_remaining = 0
        self.max_bytes_in_budget = 0
        self.update_bitrate(self.target_bitrate)
        self.timestamp_unwrapper = utils.Uint32Unwrapper()
        self.last_ms = 0

    def enqueue(self, rtp: RtpPacket, media_priority: int, layer: int = 0):
        ts = self.timestamp_unwrapper.unwrap(rtp.timestamp)
        media_queue = get_and_set_default(self.input_queue, media_priority, SortedDict())
        frame = get_and_set_default(media_queue, (layer, ts), list())
        frame.append(rtp)

    async def read_queue(self) -> RtpPacket:
        return await self.output_queue.get()

    def update_bitrate(self, bitrate: int):
        self.target_bitrate = bitrate * 1.1
        self.max_bytes_in_budget = 0.5 * self.target_bitrate / 8
        self.bytes_remaining = min(max(-self.max_bytes_in_budget, self.bytes_remaining), self.max_bytes_in_budget)

    async def run(self, now_ms):
        if self.last_ms == 0:
            self.last_ms = now_ms
            return

        delta_ms = now_ms - self.last_ms
        self.last_ms = now_ms
        self.__add_budget((self.target_bitrate / 8.) * delta_ms / 1000.)
        print("run {}, remain={}".format(now_ms, self.bytes_remaining))
        frame = list()
        layer = 0
        ts = 0
        while self.bytes_remaining > 0:
            if len(frame) == 0:
                (layer, ts), frame = self.__pop_highest_priority_frame()
            if len(frame) == 0:
                break
            pkt = frame.pop(0)
            await self.output_queue.put(pkt)
            self.__use_budget(len(pkt.payload))  # TODO: add overhead size
            print("{}, remain={}, output={}".format(now_ms, self.bytes_remaining, pkt.sequence_number))
        if len(frame) > 0 and self.bytes_remaining <= 0:  # must be video
            assert ts > 0
            priority = (max(layer - 1, 0), ts)
            video_queue = get_and_set_default(self.input_queue, MediaPriority.VIDEO, SortedDict())
            video_queue[priority] = frame
            print("{}, video frame {}, raise priority {} -> {}".format(now_ms, frame, layer, priority[0]))

    def __drop_old_frame(self):
        lowest = self.__get_lowest_priority_queue_key()
        queue = self.input_queue[lowest]
        # TODO: pop some old frame
        while len(queue) > 10:
            queue.popitem(last=False)

    def __get_lowest_priority_queue_key(self):
        for key in reversed(self.input_queue):
            if len(self.input_queue[key]) > 0:
                return key
        return None

    def __pop_highest_priority_frame(self):
        for key in self.input_queue:
            if len(self.input_queue[key]) > 0:
                tag, frame = self.input_queue[key].popitem(index=0)
                return tag, frame
        return (0, 0), list()

    def __add_budget(self, data_bytes):
        if self.bytes_remaining < 0:
            self.bytes_remaining = min(self.bytes_remaining + data_bytes, self.max_bytes_in_budget)
        else:
            self.bytes_remaining = min(data_bytes, self.max_bytes_in_budget)  # no accumulate

    def __use_budget(self, data_bytes):
        self.bytes_remaining = max(self.bytes_remaining - data_bytes, -self.max_bytes_in_budget)
