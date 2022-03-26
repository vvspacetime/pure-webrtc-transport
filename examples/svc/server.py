import asyncio
from transport import RTCPeerConnection, LocalStreamTrack, MediaStreamTrack, RtpPacket, RTCSessionDescription, codecs, \
    RTCConfiguration
from aiohttp import web, web_request, web_response
import logging
from content import Vp8PayloadDescriptor, Vp9PayloadDescriptor
from datetime import datetime
from policy import SendSideDelayBasedBitrateEstimator


pc = RTCPeerConnection()
codecs.init_codecs()
logging.basicConfig(level=logging.WARNING)


async def handle(request: web_request.Request):
    offer = await request.text()
    local_video_track = LocalStreamTrack(kind="video")
    pc.addTransceiver(local_video_track, direction="sendonly")

    remote_audio_track = None
    remote_video_track = None
    bwe = SendSideDelayBasedBitrateEstimator()

    def on_remote_track(track: MediaStreamTrack):
        nonlocal remote_audio_track, remote_video_track
        print("on track", track.kind)
        if track.kind == "audio":
            remote_audio_track = track
        else:
            remote_video_track = track

    def read_feedback_loop(local_track: LocalStreamTrack):
        async def func():
            while True:
                tcc_results = await local_track.read_feedback()
                # print("result: {}", tcc_results)
                tcc_results.sort(key=lambda e: e.receive_ms)
                for res in tcc_results:
                    print("+++++++++++++++++++++++")
                    bitrate = bwe.add(res.receive_ms, res.send_ms, res.payload_size)
                    if bitrate:
                        print("bitrate={}, recv_time={}".format(bitrate, res.receive_ms))
                    print("-----------------------")
        return func

    def echo(local_track: LocalStreamTrack, remote_track: MediaStreamTrack):
        async def func():
            while True:
                packet = await remote_track.recv()

                if len(packet.payload) != 0 and packet.payload_type == 98:
                    content = Vp9PayloadDescriptor.parse(packet.payload)
                    # print("{}, tid={}, sid={}, picid={}".format(datetime.now(), content.tid, content.sid,
                    # content.picture_id))
                    if content.tid and content.tid > 0:
                        continue

                # print("packet recv, pts={}, len={}".format(packet.timestamp, len(packet.payload)))

                await local_track.send(RtpPacket(timestamp=packet.timestamp,
                                                 payload=packet.payload, marker=packet.marker))

        return func

    def on_state_change():
        print(pc.connectionState)

    pc.add_listener("track", on_remote_track)
    pc.add_listener("connectionstatechange", on_state_change)

    offer_sd = RTCSessionDescription(sdp=offer, type="offer")
    await pc.setRemoteDescription(offer_sd)
    answer_sd = await pc.createAnswer()
    await pc.setLocalDescription(answer_sd)

    asyncio.ensure_future(echo(local_video_track, remote_video_track)())
    asyncio.ensure_future(read_feedback_loop(local_video_track)())
    return web.Response(text=answer_sd.sdp)


async def index(request):
    return web.FileResponse("./index.html")


app = web.Application()
app.add_routes([web.post("/sdp", handler=handle), web.get("/", handler=index)])

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    web.run_app(app, port=8989, loop=loop)
