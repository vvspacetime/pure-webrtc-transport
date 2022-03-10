import asyncio
from transport import RTCPeerConnection, LocalStreamTrack, MediaStreamTrack, RtpPacket, RTCSessionDescription, codecs
from aiohttp import web, web_request, web_response
import logging

pc = RTCPeerConnection()
codecs.init_codecs()
logging.basicConfig(level=logging.WARNING)


async def handle(request: web_request.Request):
    offer = await request.text()
    local_audio_track = LocalStreamTrack(kind="audio")
    local_video_track = LocalStreamTrack(kind="video")
    pc.addTransceiver(local_audio_track, direction="sendonly")
    pc.addTransceiver(local_video_track, direction="sendonly")

    remote_audio_track = None
    remote_video_track = None

    def on_remote_track(track: MediaStreamTrack):
        nonlocal remote_audio_track, remote_video_track
        print("on track", track.kind)
        if track.kind == "audio":
            remote_audio_track = track
        else:
            remote_video_track = track

    def echo(local_track: LocalStreamTrack, remote_track: MediaStreamTrack):
        async def func():
            while True:
                packet = await remote_track.recv()
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

    asyncio.ensure_future(echo(local_audio_track, remote_audio_track)())
    asyncio.ensure_future(echo(local_video_track, remote_video_track)())
    return web.Response(text=answer_sd.sdp)


async def index(request):
    return web.FileResponse("./index.html")


app = web.Application()
app.add_routes([web.post("/sdp", handler=handle), web.get("/", handler=index)])

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    web.run_app(app, port=8989, loop=loop)
