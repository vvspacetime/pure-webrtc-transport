# Pure WebRTC Transport(纯粹的WebRTC传输通道)

## What is `pure-webrtc-transport`?
This library focuses on sending and receiving RTP packets according to webrtc standard, and ignore codec.   
It can apply to both clients and SFU(Selective Forwarding Unit) servers.

## Sample
Use audio track to transmit arbitrary data
```python
from typing import Optional
import asyncio
from transport import RTCPeerConnection, LocalStreamTrack, MediaStreamTrack, RtpPacket

remote_track: Optional[MediaStreamTrack] = None
pc1 = RTCPeerConnection()
pc2 = RTCPeerConnection()
def on_track(track: MediaStreamTrack):
    nonlocal remote_track
    remote_track = track
pc2.add_listener("track", on_track)

audio_track = LocalStreamTrack(kind="audio")
pc1.addTransceiver(audio_track)
offer = await pc1.createOffer()
await pc1.setLocalDescription(offer)
await pc2.setRemoteDescription(offer)
answer = await pc2.createAnswer()
await pc1.setRemoteDescription(answer)

await asyncio.sleep(2) # wait dtls handshake
packet_to_send = b"this is a packet"
rtp_timestamp = 100
await audio_track.send(RtpPacket(timestamp=rtp_timestamp, payload=packet_to_send))
recv_packet = await remote_track.recv()
print("recv payload", recv_packet.payload)
```

## Examples
- [x] Echo Server
- [ ] SFU
- [ ] WebRTC Benchmark Tool

## License
The source code based on `aiortc`, and some code refactoring.




