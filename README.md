# Pure WebRTC Transport(纯粹的WebRTC传输通道)
[README in English](README.en.md)

## What is `pure-webrtc-transport`?
### 传统的WebRTC实现
WebRTC是一个包含音视频的采集、编解码、网络传输、显示的复杂标准。  
`aiortc`实现了下图中的过程.
```
+--------+        +-----------+        +------+
| encode | -----> | pacing/cc | -----> | send | ----+
+--------+        +-----------+        +------+     |
                                                   network
+--------+        +-----------+        +------+     |
| decode | <----- | jitterbuf | <----- | recv | <---+
+--------+        +-----------+        +------+

```
`webrtc native`源码实现的过程更加全面和复杂，将音视频和网络传输的概念耦合在一起，
这使得将`webrtc native`迁移到`SFU`服务器端使用需要大量的改造。  

另外`webrtc`的大多数开源实现中都将机制和算法策略糅合在一起，这使得在开发者在针对不同场景调节策略时，
不可避免的需要了解，甚至修改一些机制上的代码。这对模块化，单元测试以及迭代测试都是有害无益的。 
#### 机制和策略：
`NACK生成算法`、`GCC算法`、`JitterBuffer`、`将视频帧编码后传输`等属于算法策略。  
`解析SDP`、`生成DTLS传输通道`、`传输RTP/RTCP包`等属于机制。

### pure-webrtc-transport
`pure-webrtc-transport`(纯粹的WebRTC传输通道)与传统的开源WebRTC实现不同。`pure-webrtc-transport`和UDP协议相似，只提供`网络收发`的机制，
而将`传输算法`，`抖动缓冲`、`编解码`、`打包`等交给上层来做。  
这使得算法策略模块的修改不会影响基本的网络收发功能，使其更容易替换和对比。传输功能也独立于音视频内容，使其更易理解和测试。  
`pure-webrtc-transport`作为一个纯粹的传输通道，可以同时在客户端和服务器（例如：WebRTC SFU）使用。

## Sample
使用音频track传输任意数据
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

## License
The source code based on `aiortc`, and some code refactoring.




