import asyncio
from transport import RTCPeerConnection, LocalStreamTrack, MediaStreamTrack, RtpPacket, RTCSessionDescription, codecs, \
    RTCConfiguration, rtp, RemoteStreamTrack, clock
from aiohttp import web, web_request, web_response
import logging
from content import Vp8PayloadDescriptor, Vp9PayloadDescriptor
from datetime import datetime
from policy import SendSideDelayBasedBitrateEstimator, TemporalLayerFilter, Pacer
from typing import Optional
import traceback

pc = RTCPeerConnection()
codecs.init_codecs()
logging.basicConfig(level=logging.WARNING)


class SvcRelayer:
    def __init__(self):
        self.rx_pc = RTCPeerConnection()
        self.tx_pc = RTCPeerConnection()
        self.rx_track = Optional[RemoteStreamTrack]
        self.tx_track = Optional[LocalStreamTrack]
        self.rx_done = False
        self.tx_done = False
        self.bwe = SendSideDelayBasedBitrateEstimator()
        self.filter = TemporalLayerFilter()
        self.pacer = Pacer()

    async def run_relay(self):
        print("waiting done: {}, {}".format(self.rx_done, self.tx_done))

        if not (self.rx_done and self.tx_done):
            return
        asyncio.ensure_future(self.relay())
        asyncio.ensure_future(self.read_feedback_loop())
        asyncio.ensure_future(self.pacing())

    async def relay(self):
        while True:
            packet = await self.rx_track.recv()

            if len(packet.payload) != 0 and packet.payload_type == 98:
                content = Vp9PayloadDescriptor.parse(packet.payload)
                do_pass = self.filter.add_video_sample(flow_id=0, layer=content.tid, data_bytes=len(packet.payload),
                                                       now_ms=clock.current_ms())
                if do_pass:
                    self.pacer.enqueue(RtpPacket(timestamp=packet.timestamp,
                                                 payload=packet.payload, marker=packet.marker))

    async def pacing(self):
        while True:
            packet: RtpPacket = await self.pacer.read_queue()
            await self.tx_track.send(packet)

    async def read_feedback_loop(self):
        try:
            while True:
                pkt = await self.tx_track.read_feedback()
                if isinstance(pkt, rtp.RtcpPsfbPacket) and pkt.fmt == rtp.RTCP_PSFB_PLI:
                    # print("rtcp pli")
                    await self.rx_track.send_feedback(pkt)
                elif isinstance(pkt, rtp.RtcpRtpfbPacket):
                    # print("rtcp twcc, result: {}".format(pkt.twcc))
                    pkt.twcc.sort(key=lambda e: e.receive_ms)
                    for res in pkt.twcc:
                        if res.received and res.send_ms:
                            # print("=======================")
                            bitrate = self.bwe.add(res.receive_ms, res.send_ms, res.payload_size)
                            if bitrate:
                                # print("bitrate={}, recv_time={}".format(bitrate, res.receive_ms))
                                self.filter.update_available_bitrate(int(bitrate))
                                self.pacer.update_bitrate(int(bitrate))
                            # print("=======================\n")
        except Exception as e:
            print("read feedback loop stopped: {}".format(traceback.format_exc()))
        finally:
            print("read feedback loop stopped")

    async def handle_send_side_sdp(self, request: web_request.Request):
        if self.rx_done:
            return
        self.rx_done = True

        pc = self.rx_pc
        offer = await request.text()

        def on_remote_track(track: MediaStreamTrack):
            print("on track", track.kind)
            if track.kind == "video":
                self.rx_track = track

        pc.add_listener("track", on_remote_track)
        offer_sd = RTCSessionDescription(sdp=offer, type="offer")
        await pc.setRemoteDescription(offer_sd)
        answer_sd = await pc.createAnswer()
        await pc.setLocalDescription(answer_sd)
        asyncio.ensure_future(self.run_relay())
        return web.Response(text=answer_sd.sdp)

    async def handle_recv_side_sdp(self, request: web_request.Request):
        if self.tx_done:
            return
        self.tx_done = True

        pc = self.tx_pc
        offer = await request.text()
        self.tx_track = LocalStreamTrack(kind="video")
        pc.addTransceiver(self.tx_track, direction="sendonly")

        offer_sd = RTCSessionDescription(sdp=offer, type="offer")
        await pc.setRemoteDescription(offer_sd)
        answer_sd = await pc.createAnswer()
        await pc.setLocalDescription(answer_sd)
        asyncio.ensure_future(self.run_relay())
        return web.Response(text=answer_sd.sdp)


async def send_index(request):
    return web.FileResponse("send.html")


async def recv_index(request):
    return web.FileResponse("recv.html")


app = web.Application()
svc_relayer = SvcRelayer()
app.add_routes([web.post("/send/sdp", handler=svc_relayer.handle_send_side_sdp),
                web.post("/recv/sdp", handler=svc_relayer.handle_recv_side_sdp),
                web.get("/send", handler=send_index),
                web.get("/recv", handler=recv_index)])
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    web.run_app(app, port=8989, loop=loop)
