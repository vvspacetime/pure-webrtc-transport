import unittest
import asyncio
import logging
from policy import PriorityPacer, MediaPriority
from transport import RtpPacket


class TestPriorityPacerCase(unittest.TestCase):
    def test_priority_pacer(self):
        logging.basicConfig(level=logging.WARNING)

        async def order_test():
            pacer = PriorityPacer()
            pacer.update_bitrate(8000)  # 1000byte/s
            pacer.enqueue(rtp=RtpPacket(payload=b'0' * 20, sequence_number=0),
                          media_priority=MediaPriority.AUDIO, layer=0)
            pacer.enqueue(rtp=RtpPacket(payload=b'1' * 20, sequence_number=1),
                          media_priority=MediaPriority.RTX, layer=0)

            pacer.enqueue(rtp=RtpPacket(payload=b'2' * 20, sequence_number=2),
                          media_priority=MediaPriority.VIDEO, layer=1)
            pacer.enqueue(rtp=RtpPacket(payload=b'3' * 20, sequence_number=3),
                          media_priority=MediaPriority.VIDEO, layer=2)
            pacer.enqueue(rtp=RtpPacket(payload=b'4' * 20, sequence_number=4),
                          media_priority=MediaPriority.VIDEO, layer=0)

            await pacer.run(1)
            await pacer.run(101)  # 110 bytes

            p1 = await pacer.read_queue()
            p2 = await pacer.read_queue()
            p3 = await pacer.read_queue()
            p4 = await pacer.read_queue()
            p5 = await pacer.read_queue()

            self.assertTrue(p1.sequence_number == 1)
            self.assertTrue(p2.sequence_number == 0)
            self.assertTrue(p3.sequence_number == 4)
            self.assertTrue(p4.sequence_number == 2)
            self.assertTrue(p5.sequence_number == 3)

        async def video_layer():
            pacer = PriorityPacer()
            pacer.update_bitrate(8000)  # 1000byte/s
            pacer.enqueue(rtp=RtpPacket(payload=b'4' * 20, timestamp=1, sequence_number=100),
                          media_priority=MediaPriority.VIDEO, layer=1)
            pacer.enqueue(rtp=RtpPacket(payload=b'4' * 20, timestamp=1, sequence_number=101),
                          media_priority=MediaPriority.VIDEO, layer=1)
            pacer.enqueue(rtp=RtpPacket(payload=b'5' * 20, timestamp=2, sequence_number=102),
                          media_priority=MediaPriority.VIDEO, layer=2)
            pacer.enqueue(rtp=RtpPacket(payload=b'3' * 20, timestamp=3, sequence_number=103),
                          media_priority=MediaPriority.VIDEO, layer=0)
            await pacer.run(1)
            await pacer.run(31)  # 33 bytes
            pacer.enqueue(rtp=RtpPacket(payload=b'3' * 20, timestamp=4, sequence_number=104),
                          media_priority=MediaPriority.VIDEO, layer=0)

            await pacer.run(101)
            p1 = await pacer.read_queue()
            p2 = await pacer.read_queue()
            p3 = await pacer.read_queue()
            p4 = await pacer.read_queue()
            p5 = await pacer.read_queue()
            print(p1, p2, p3, p4, p5)

        asyncio.run(order_test())
        asyncio.run(video_layer())


if __name__ == '__main__':
    unittest.main()
