import unittest
from typing import Optional
import asyncio
from transport import RTCPeerConnection, LocalStreamTrack, MediaStreamTrack, RtpPacket
import logging


class TestTransportCase(unittest.TestCase):
    def test_peerconnection(self):
        logging.basicConfig(level=logging.WARNING)

        async def internal_test():
            remote_track: Optional[MediaStreamTrack] = None
            pc1 = RTCPeerConnection()
            pc2 = RTCPeerConnection()
            # print("pc1, pc2", pc1, pc2)

            def on_track(track: MediaStreamTrack):
                nonlocal remote_track
                # print("on track", track.id, track.kind)
                remote_track = track

            pc2.add_listener("track", on_track)

            audio_track = LocalStreamTrack(kind="audio")
            pc1.addTransceiver(audio_track)
            offer = await pc1.createOffer()
            # print("offer", offer.sdp)

            await pc1.setLocalDescription(offer)
            await pc2.setRemoteDescription(offer)
            answer = await pc2.createAnswer()
            # print("answer", answer.sdp)

            await pc1.setRemoteDescription(answer)
            self.assertTrue(remote_track is not None, "check track")

            await asyncio.sleep(2)

            packet_to_send = b"this is a packet"
            rtp_timestamp = 100
            await audio_track.send(RtpPacket(timestamp=rtp_timestamp, payload=packet_to_send))
            recv_packet = await remote_track.recv()
            print("recv payload", recv_packet.payload)
            self.assertTrue(recv_packet.timestamp == rtp_timestamp, "check rtp timestamp")
            self.assertTrue(recv_packet.payload == packet_to_send, "check rtp payload")

        asyncio.run(internal_test())


if __name__ == '__main__':
    unittest.main()
