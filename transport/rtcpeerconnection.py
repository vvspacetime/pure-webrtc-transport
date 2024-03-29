import asyncio
import copy
import logging
import uuid
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Union

from pyee.asyncio import AsyncIOEventEmitter

from . import clock
from . import rtp
from . import sdp
from .events import RTCTrackEvent
from .exceptions import (
    InternalError,
    InvalidAccessError,
    InvalidStateError,
    OperationError,
)
from .mediastreams import MediaStreamTrack
from .rtcconfiguration import RTCConfiguration
from .rtcdtlstransport import RTCCertificate, RTCDtlsParameters, RTCDtlsTransport
from .rtcicetransport import (
    RTCIceCandidate,
    RTCIceGatherer,
    RTCIceParameters,
    RTCIceTransport,
)
from .rtcrtpparameters import (
    RTCRtpCodecCapability,
    RTCRtpCodecParameters,
    RTCRtpDecodingParameters,
    RTCRtpHeaderExtensionParameters,
    RTCRtpParameters,
    RTCRtpReceiveParameters,
    RTCRtpRtxParameters,
    RTCRtpSendParameters,
)
from .rtcrtpreceiver import RemoteStreamTrack, RTCRtpReceiver
from .rtcrtpsender import RTCRtpSender
from .rtcrtptransceiver import RTCRtpTransceiver
from .rtcsessiondescription import RTCSessionDescription
from .stats import RTCStatsReport
from .codecs import is_rtx, HEADER_EXTENSIONS, CODECS

DISCARD_HOST = "0.0.0.0"
DISCARD_PORT = 9
MEDIA_KINDS = ["audio", "video"]

logger = logging.getLogger(__name__)


def filter_preferred_codecs(
    codecs: List[RTCRtpCodecParameters], preferred: List[RTCRtpCodecCapability]
) -> List[RTCRtpCodecParameters]:
    if not preferred:
        return codecs

    rtx_codecs = list(filter(is_rtx, codecs))
    rtx_enabled = next(filter(is_rtx, preferred), None) is not None

    filtered = []
    for pref in filter(lambda x: not is_rtx(x), preferred):
        for codec in codecs:
            if (
                codec.mimeType.lower() == pref.mimeType.lower()
                and codec.parameters == pref.parameters
            ):
                filtered.append(codec)

                # add corresponding RTX
                if rtx_enabled:
                    for rtx in rtx_codecs:
                        if rtx.parameters["apt"] == codec.payloadType:
                            filtered.append(rtx)
                            break

                break

    return filtered


def find_common_codecs(
    local_codecs: List[RTCRtpCodecParameters],
    remote_codecs: List[RTCRtpCodecParameters],
) -> List[RTCRtpCodecParameters]:
    common = []
    common_base: Dict[int, RTCRtpCodecParameters] = {}
    for c in remote_codecs:
        # for RTX, check we accepted the base codec
        if is_rtx(c):
            if c.parameters.get("apt") in common_base:
                base = common_base[c.parameters["apt"]]
                if c.clockRate == base.clockRate:
                    common.append(copy.deepcopy(c))
            continue

        # handle other codecs
        for codec in local_codecs:
            if (
                codec.mimeType.lower() == c.mimeType.lower()
                and codec.clockRate == c.clockRate
            ):
                if codec.mimeType.lower() == "video/h264":
                    # FIXME: check according to RFC 6184
                    parameters_compatible = True
                    for param in ["packetization-mode", "profile-level-id"]:
                        if c.parameters.get(param) != codec.parameters.get(param):
                            parameters_compatible = False
                    if not parameters_compatible:
                        continue

                codec = copy.deepcopy(codec)
                if c.payloadType in rtp.DYNAMIC_PAYLOAD_TYPES:
                    codec.payloadType = c.payloadType
                codec.rtcpFeedback = list(
                    filter(lambda x: x in c.rtcpFeedback, codec.rtcpFeedback)
                )
                common.append(codec)
                common_base[codec.payloadType] = codec
                break
    return common


def find_common_header_extensions(
    local_extensions: List[RTCRtpHeaderExtensionParameters],
    remote_extensions: List[RTCRtpHeaderExtensionParameters],
) -> List[RTCRtpHeaderExtensionParameters]:
    common = []
    # NOTES: 匹配并使用远端的extension id
    for rx in remote_extensions:
        for lx in local_extensions:
            if lx.uri == rx.uri:
                common.append(rx)
    return common


def add_transport_description(
    media: sdp.MediaDescription, dtlsTransport: RTCDtlsTransport
) -> None:
    # ice
    iceTransport = dtlsTransport.transport
    iceGatherer = iceTransport.iceGatherer
    media.ice_candidates = iceGatherer.getLocalCandidates()
    media.ice_candidates_complete = iceGatherer.state == "completed"
    media.ice = iceGatherer.getLocalParameters()
    if media.ice_candidates:
        media.host = media.ice_candidates[0].ip
        media.port = media.ice_candidates[0].port
    else:
        media.host = DISCARD_HOST
        media.port = DISCARD_PORT

    # dtls
    if media.dtls is None:
        media.dtls = dtlsTransport.getLocalParameters()
    else:
        media.dtls.fingerprints = dtlsTransport.getLocalParameters().fingerprints


async def add_remote_candidates(
    iceTransport: RTCIceTransport, media: sdp.MediaDescription
) -> None:
    coros = map(iceTransport.addRemoteCandidate, media.ice_candidates)
    await asyncio.gather(*coros)

    if media.ice_candidates_complete:
        await iceTransport.addRemoteCandidate(None)


def allocate_mid(mids: Set[str]) -> str:
    """
    Allocate a MID which has not been used yet.
    """
    i = 0
    while True:
        mid = str(i)
        if mid not in mids:
            mids.add(mid)
            return mid
        i += 1


def create_media_description_for_transceiver(
    transceiver: RTCRtpTransceiver, cname: str, direction: str, mid: str
) -> sdp.MediaDescription:
    media = sdp.MediaDescription(
        kind=transceiver.kind,
        port=DISCARD_PORT,
        profile="UDP/TLS/RTP/SAVPF",
        fmt=[c.payloadType for c in transceiver._codecs],
    )
    media.direction = direction
    media.msid = f"{transceiver.sender._stream_id} {transceiver.sender._track_id}"

    media.rtp = RTCRtpParameters(
        codecs=transceiver._codecs,
        headerExtensions=transceiver._headerExtensions,
        muxId=mid,
    )
    media.rtcp_host = DISCARD_HOST
    media.rtcp_port = DISCARD_PORT
    media.rtcp_mux = True

    # Only sender need ssrc
    if media.direction in ["sendonly", "sendrecv"]:
        media.ssrc = [sdp.SsrcDescription(ssrc=transceiver.sender._ssrc, cname=cname)]

        # if RTX is enabled, add corresponding SSRC
        if next(filter(is_rtx, media.rtp.codecs), None):
            media.ssrc.append(
                sdp.SsrcDescription(ssrc=transceiver.sender._rtx_ssrc, cname=cname)
            )
            media.ssrc_group = [
                sdp.GroupDescription(
                    semantic="FID",
                    items=[transceiver.sender._ssrc, transceiver.sender._rtx_ssrc],
                )
            ]

    add_transport_description(media, transceiver._transport)

    return media


def and_direction(a: str, b: str) -> str:
    return sdp.DIRECTIONS[sdp.DIRECTIONS.index(a) & sdp.DIRECTIONS.index(b)]


def or_direction(a: str, b: str) -> str:
    return sdp.DIRECTIONS[sdp.DIRECTIONS.index(a) | sdp.DIRECTIONS.index(b)]


def reverse_direction(direction: str) -> str:
    if direction == "sendonly":
        return "recvonly"
    elif direction == "recvonly":
        return "sendonly"
    return direction


def wrap_session_description(
    session_description: Optional[sdp.SessionDescription],
) -> Optional[RTCSessionDescription]:
    if session_description is not None:
        return RTCSessionDescription(
            sdp=str(session_description), type=session_description.type
        )
    return None


class RTCPeerConnection(AsyncIOEventEmitter):
    """
    The :class:`RTCPeerConnection` interface represents a WebRTC connection
    between the local computer and a remote peer.

    :param configuration: An optional :class:`RTCConfiguration`.
    """

    def __init__(self, configuration: Optional[RTCConfiguration] = None) -> None:
        super().__init__()
        self.__certificates = [RTCCertificate.generateCertificate()]
        self.__cname = f"{uuid.uuid4()}"
        self.__configuration = configuration or RTCConfiguration()
        self.__dtlsTransports: Set[RTCDtlsTransport] = set()
        self.__iceTransports: Set[RTCIceTransport] = set()
        self.__remoteDtls: Dict[RTCRtpTransceiver, RTCDtlsParameters] = {}
        self.__remoteIce: Dict[RTCRtpTransceiver, RTCIceParameters] = {}
        self.__seenMids: Set[str] = set()
        self.__stream_id = str(uuid.uuid4())
        self.__transceivers: List[RTCRtpTransceiver] = []

        self.__connectionState = "new"
        self.__iceConnectionState = "new"
        self.__iceGatheringState = "new"
        self.__isClosed = False
        self.__signalingState = "stable"

        self.__currentLocalDescription: Optional[sdp.SessionDescription] = None
        self.__currentRemoteDescription: Optional[sdp.SessionDescription] = None
        self.__pendingLocalDescription: Optional[sdp.SessionDescription] = None
        self.__pendingRemoteDescription: Optional[sdp.SessionDescription] = None

    @property
    def connectionState(self) -> str:
        """
        The current connection state.

        Possible values: `"connected"`, `"connecting"`, `"closed"`, `"failed"`, `"new`".

        When the state changes, the `"connectionstatechange"` event is fired.
        """
        return self.__connectionState

    @property
    def iceConnectionState(self) -> str:
        """
        The current ICE connection state.

        Possible values: `"checking"`, `"completed"`, `"closed"`, `"failed"`, `"new`".

        When the state changes, the `"iceconnectionstatechange"` event is fired.
        """
        return self.__iceConnectionState

    @property
    def iceGatheringState(self) -> str:
        """
        The current ICE gathering state.

        Possible values: `"complete"`, `"gathering"`, `"new`".

        When the state changes, the `"icegatheringstatechange"` event is fired.
        """
        return self.__iceGatheringState

    @property
    def localDescription(self) -> RTCSessionDescription:
        """
        An :class:`RTCSessionDescription` describing the session for
        the local end of the connection.
        """
        return wrap_session_description(self.__localDescription())

    @property
    def remoteDescription(self) -> RTCSessionDescription:
        """
        An :class:`RTCSessionDescription` describing the session for
        the remote end of the connection.
        """
        return wrap_session_description(self.__remoteDescription())

    @property
    def signalingState(self):
        """
        The current signaling state.

        Possible values: `"closed"`, `"have-local-offer"`, `"have-remote-offer`", `"stable"`.

        When the state changes, the `"signalingstatechange"` event is fired.
        """
        return self.__signalingState

    async def addIceCandidate(self, candidate: RTCIceCandidate) -> None:
        """
        Add a new :class:`RTCIceCandidate` received from the remote peer.

        The specified candidate must have a value for either `sdpMid` or `sdpMLineIndex`.

        :param candidate: The new remote candidate.
        """
        if candidate.sdpMid is None and candidate.sdpMLineIndex is None:
            raise ValueError("Candidate must have either sdpMid or sdpMLineIndex")

        for transceiver in self.__transceivers:
            if candidate.sdpMid == transceiver.mid and not transceiver._bundled:
                iceTransport = transceiver._transport.transport
                await iceTransport.addRemoteCandidate(candidate)
                return

    def addTrack(self, track: MediaStreamTrack) -> RTCRtpSender:
        """
        Add a :class:`MediaStreamTrack` to the set of media tracks which
        will be transmitted to the remote peer.
        """
        # check state is valid
        self.__assertNotClosed()
        if track.kind not in ["audio", "video"]:
            raise InternalError(f'Invalid track kind "{track.kind}"')

        # don't add track twice
        self.__assertTrackHasNoSender(track)

        for transceiver in self.__transceivers:
            if transceiver.kind == track.kind:
                if transceiver.sender.track is None:
                    transceiver.sender.replaceTrack(track)
                    transceiver.direction = or_direction(
                        transceiver.direction, "sendonly"
                    )
                    return transceiver.sender

        transceiver = self.__createTransceiver(
            direction="sendrecv", kind=track.kind, sender_track=track
        )
        return transceiver.sender

    def addTransceiver(
        self, trackOrKind: Union[str, MediaStreamTrack], direction: str = "sendrecv"
    ) -> RTCRtpTransceiver:
        """
        Add a new :class:`RTCRtpTransceiver`.
        """
        self.__assertNotClosed()

        # determine track or kind
        if isinstance(trackOrKind, MediaStreamTrack):
            kind = trackOrKind.kind
            track = trackOrKind
        else:
            kind = trackOrKind
            track = None
        if kind not in ["audio", "video"]:
            raise InternalError(f'Invalid track kind "{kind}"')

        # check direction
        if direction not in sdp.DIRECTIONS:
            raise InternalError(f'Invalid direction "{direction}"')

        # don't add track twice
        if track:
            self.__assertTrackHasNoSender(track)

        return self.__createTransceiver(
            direction=direction, kind=kind, sender_track=track
        )

    async def close(self):
        """
        Terminate the ICE agent, ending ICE processing and streams.
        """
        if self.__isClosed:
            return
        self.__isClosed = True
        self.__setSignalingState("closed")

        # stop senders / receivers
        for transceiver in self.__transceivers:
            await transceiver.stop()

        # stop transports
        for transceiver in self.__transceivers:
            await transceiver._transport.stop()
            await transceiver._transport.transport.stop()

        # update states
        self.__updateIceGatheringState()
        self.__updateIceConnectionState()
        self.__updateConnectionState()

        # no more events will be emitted, so remove all event listeners
        # to facilitate garbage collection.
        self.remove_all_listeners()

    async def createAnswer(self):
        """
        Create an SDP answer to an offer received from a remote peer during
        the offer/answer negotiation of a WebRTC connection.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()
        await self.__gather()

        if self.signalingState not in ["have-remote-offer", "have-local-pranswer"]:
            raise InvalidStateError(
                f'Cannot create answer in signaling state "{self.signalingState}"'
            )

        # create description
        ntp_seconds = clock.current_ntp_time() >> 32
        description = sdp.SessionDescription()
        description.origin = f"- {ntp_seconds} {ntp_seconds} IN IP4 0.0.0.0"
        description.msid_semantic.append(
            sdp.GroupDescription(semantic="WMS", items=["*"])
        )
        description.type = "answer"

        for remote_m in self.__remoteDescription().media:
            if remote_m.kind in ["audio", "video"]:
                transceiver = self.__getTransceiverByMid(remote_m.rtp.muxId)
                media = create_media_description_for_transceiver(
                    transceiver,
                    cname=self.__cname,
                    direction=and_direction(
                        transceiver.direction, transceiver._offerDirection
                    ),
                    mid=transceiver.mid,
                )
                dtlsTransport = transceiver._transport

            # determine DTLS role, or preserve the currently configured role
            if dtlsTransport._role == "auto":
                media.dtls.role = "client"
            else:
                media.dtls.role = dtlsTransport._role

            description.media.append(media)

        bundle = sdp.GroupDescription(semantic="BUNDLE", items=[])
        for media in description.media:
            bundle.items.append(media.rtp.muxId)
        description.group.append(bundle)

        return wrap_session_description(description)

    async def createOffer(self) -> RTCSessionDescription:
        """
        Create an SDP offer for the purpose of starting a new WebRTC
        connection to a remote peer.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()
        await self.__gather()

        # offer codecs
        for transceiver in self.__transceivers:
            transceiver._codecs = filter_preferred_codecs(
                CODECS[transceiver.kind][:], transceiver._preferred_codecs
            )
            transceiver._headerExtensions = HEADER_EXTENSIONS[transceiver.kind][:]

        mids = self.__seenMids.copy()

        # create description
        ntp_seconds = clock.current_ntp_time() >> 32
        description = sdp.SessionDescription()
        description.origin = f"- {ntp_seconds} {ntp_seconds} IN IP4 0.0.0.0"
        description.msid_semantic.append(
            sdp.GroupDescription(semantic="WMS", items=["*"])
        )
        description.type = "offer"

        def get_media(
            description: sdp.SessionDescription,
        ) -> List[sdp.MediaDescription]:
            return description.media if description else []

        def get_media_section(
            media: List[sdp.MediaDescription], i: int
        ) -> Optional[sdp.MediaDescription]:
            return media[i] if i < len(media) else None

        # handle existing transceivers
        local_media = get_media(self.__localDescription())
        remote_media = get_media(self.__remoteDescription())
        for i in range(max(len(local_media), len(remote_media))):
            local_m = get_media_section(local_media, i)
            remote_m = get_media_section(remote_media, i)
            media_kind = local_m.kind if local_m else remote_m.kind
            mid = local_m.rtp.muxId if local_m else remote_m.rtp.muxId
            if media_kind in ["audio", "video"]:
                transceiver = self.__getTransceiverByMid(mid)
                transceiver._set_mline_index(i)
                description.media.append(
                    create_media_description_for_transceiver(
                        transceiver,
                        cname=self.__cname,
                        direction=transceiver.direction,
                        mid=mid,
                    )
                )

        # handle new transceivers
        def next_mline_index() -> int:
            return len(description.media)

        for transceiver in filter(
            lambda x: x.mid is None and not x.stopped, self.__transceivers
        ):
            transceiver._set_mline_index(next_mline_index())
            description.media.append(
                create_media_description_for_transceiver(
                    transceiver,
                    cname=self.__cname,
                    direction=transceiver.direction,
                    mid=allocate_mid(mids),
                )
            )

        bundle = sdp.GroupDescription(semantic="BUNDLE", items=[])
        for media in description.media:
            bundle.items.append(media.rtp.muxId)
        description.group.append(bundle)

        return wrap_session_description(description)

    def getReceivers(self) -> List[RTCRtpReceiver]:
        """
        Returns the list of :class:`RTCRtpReceiver` objects that are currently
        attached to the connection.
        """
        return list(map(lambda x: x.receiver, self.__transceivers))

    def getSenders(self) -> List[RTCRtpSender]:
        """
        Returns the list of :class:`RTCRtpSender` objects that are currently
        attached to the connection.
        """
        return list(map(lambda x: x.sender, self.__transceivers))

    async def getStats(self) -> RTCStatsReport:
        """
        Returns statistics for the connection.

        :rtype: :class:`RTCStatsReport`
        """
        merged = RTCStatsReport()
        coros = [x.getStats() for x in self.getSenders()] + [
            x.getStats() for x in self.getReceivers()
        ]
        for report in await asyncio.gather(*coros):
            merged.update(report)
        return merged

    def getTransceivers(self) -> List[RTCRtpTransceiver]:
        """
        Returns the list of :class:`RTCRtpTransceiver` objects that are currently
        attached to the connection.
        """
        return list(self.__transceivers)

    async def setLocalDescription(
        self, sessionDescription: RTCSessionDescription
    ) -> None:
        """
        Change the local description associated with the connection.

        :param sessionDescription: An :class:`RTCSessionDescription` generated
                                    by :meth:`createOffer` or :meth:`createAnswer()`.
        """
        # parse and validate description
        description = sdp.SessionDescription.parse(sessionDescription.sdp)
        description.type = sessionDescription.type
        self.__validate_description(description, is_local=True)

        # update signaling state
        if description.type == "offer":
            self.__setSignalingState("have-local-offer")
        elif description.type == "answer":
            self.__setSignalingState("stable")

        # assign MID
        for i, media in enumerate(description.media):
            mid = media.rtp.muxId
            self.__seenMids.add(mid)
            if media.kind in ["audio", "video"]:
                transceiver = self.__getTransceiverByMLineIndex(i)
                transceiver._set_mid(mid)

        # set ICE role
        if description.type == "offer":
            for iceTransport in self.__iceTransports:
                if not iceTransport._role_set:
                    iceTransport._connection.ice_controlling = True
                    iceTransport._role_set = True

        # set DTLS role
        if description.type == "answer":
            for i, media in enumerate(description.media):
                if media.kind in ["audio", "video"]:
                    transceiver = self.__getTransceiverByMLineIndex(i)
                    transceiver._transport._set_role(media.dtls.role)

        # configure direction
        for t in self.__transceivers:
            if description.type in ["answer", "pranswer"]:
                if not t._offerDirection:
                    t._currentDirection = "inactive"
                else:
                    t._currentDirection = and_direction(t.direction, t._offerDirection)

        # gather candidates
        await self.__gather()
        for i, media in enumerate(description.media):
            if media.kind in ["audio", "video"]:
                transceiver = self.__getTransceiverByMLineIndex(i)
                add_transport_description(media, transceiver._transport)

        # connect
        asyncio.ensure_future(self.__connect())

        # replace description
        if description.type == "answer":
            self.__currentLocalDescription = description
            self.__pendingLocalDescription = None
        else:
            self.__pendingLocalDescription = description

    async def setRemoteDescription(
        self, sessionDescription: RTCSessionDescription
    ) -> None:
        """
        Changes the remote description associated with the connection.

        :param sessionDescription: An :class:`RTCSessionDescription` created from
                                    information received over the signaling channel.
        """
        # parse and validate description
        description = sdp.SessionDescription.parse(sessionDescription.sdp)
        description.type = sessionDescription.type
        self.__validate_description(description, is_local=False)

        # apply description
        iceCandidates: Dict[RTCIceTransport, sdp.MediaDescription] = {}
        trackEvents = []
        for i, media in enumerate(description.media):
            dtlsTransport: Optional[RTCDtlsTransport] = None
            self.__seenMids.add(media.rtp.muxId)
            if media.kind in ["audio", "video"]:
                # find transceiver
                transceiver = None
                for t in self.__transceivers:
                    if t.kind == media.kind and t.mid in [None, media.rtp.muxId]:
                        if t.mid or (and_direction(t.direction, reverse_direction(media.direction)) != "inactive"):
                            transceiver = t
                if transceiver is None:
                    transceiver = self.__createTransceiver(
                        direction="recvonly", kind=media.kind
                    )
                if transceiver.mid is None:
                    transceiver._set_mid(media.rtp.muxId)
                    transceiver._set_mline_index(i)

                # negotiate codecs
                common = filter_preferred_codecs(
                    find_common_codecs(CODECS[media.kind], media.rtp.codecs),
                    transceiver._preferred_codecs,
                )

                if not len(common):
                    raise OperationError(
                        "Failed to set remote {} description send parameters".format(
                            media.kind
                        )
                    )

                transceiver._codecs = common
                # NOTES: workaround use remb for uplink
                # TODO: remove
                extensions = HEADER_EXTENSIONS[media.kind].copy()
                if media.direction == "sendonly" and media.kind == "video":
                    extensions.pop()

                transceiver._headerExtensions = find_common_header_extensions(
                    extensions, media.rtp.headerExtensions
                )

                # configure direction
                direction = reverse_direction(media.direction)
                if description.type in ["answer", "pranswer"]:
                    transceiver._currentDirection = direction
                else:
                    transceiver._offerDirection = direction
                    # 设置direction, 为了可以在dtls connected时向transport注册receiver
                    transceiver._currentDirection = direction

                # create remote stream track
                if (
                    direction in ["recvonly", "sendrecv"]
                    and not transceiver.receiver.track
                ):
                    transceiver.receiver._track = RemoteStreamTrack(
                        kind=media.kind, id=description.webrtc_track_id(media)
                    )
                    trackEvents.append(
                        RTCTrackEvent(
                            receiver=transceiver.receiver,
                            track=transceiver.receiver.track,
                            transceiver=transceiver,
                        )
                    )

                # memorise transport parameters
                dtlsTransport = transceiver._transport
                self.__remoteDtls[transceiver] = media.dtls
                self.__remoteIce[transceiver] = media.ice

            if dtlsTransport is not None:
                # add ICE candidates
                iceTransport = dtlsTransport.transport
                iceCandidates[iceTransport] = media

                # set ICE role
                if description.type == "offer" and not iceTransport._role_set:
                    iceTransport._connection.ice_controlling = media.ice.iceLite
                    iceTransport._role_set = True

                # set DTLS role
                if description.type == "answer":
                    dtlsTransport._set_role(
                        role="server" if media.dtls.role == "client" else "client"
                    )

        # remove bundled transports
        bundle = next((x for x in description.group if x.semantic == "BUNDLE"), None)
        if bundle and bundle.items:
            # find main media stream
            masterMid = bundle.items[0]
            masterTransport = None
            for transceiver in self.__transceivers:
                if transceiver.mid == masterMid:
                    masterTransport = transceiver._transport
                    break

            # replace transport for bundled media
            oldTransports = set()
            slaveMids = bundle.items[1:]
            for transceiver in self.__transceivers:
                if transceiver.mid in slaveMids and not transceiver._bundled:
                    oldTransports.add(transceiver._transport)
                    transceiver.receiver.setTransport(masterTransport)
                    transceiver.sender.setTransport(masterTransport)
                    transceiver._bundled = True
                    transceiver._transport = masterTransport

            # stop and discard old ICE transports
            for dtlsTransport in oldTransports:
                await dtlsTransport.stop()
                await dtlsTransport.transport.stop()
                self.__dtlsTransports.discard(dtlsTransport)
                self.__iceTransports.discard(dtlsTransport.transport)
                iceCandidates.pop(dtlsTransport.transport, None)
            self.__updateIceGatheringState()
            self.__updateIceConnectionState()
            self.__updateConnectionState()

        # add remote candidates
        coros = [
            add_remote_candidates(iceTransport, media)
            for iceTransport, media in iceCandidates.items()
        ]
        await asyncio.gather(*coros)
        await self.__gather()

        # FIXME: in aiortc 2.0.0 emit RTCTrackEvent directly
        for event in trackEvents:
            self.emit("track", event.track)

        # connect
        asyncio.ensure_future(self.__connect())

        # update signaling state
        if description.type == "offer":
            self.__setSignalingState("have-remote-offer")
        elif description.type == "answer":
            self.__setSignalingState("stable")

        # replace description
        if description.type == "answer":
            self.__currentRemoteDescription = description
            self.__pendingRemoteDescription = None
        else:
            self.__pendingRemoteDescription = description

    async def __connect(self) -> None:
        for transceiver in self.__transceivers:
            dtlsTransport = transceiver._transport
            iceTransport = dtlsTransport.transport
            if (
                iceTransport.iceGatherer.getLocalCandidates()
                and transceiver in self.__remoteIce
            ):
                await iceTransport.start(self.__remoteIce[transceiver])
                if dtlsTransport.state == "new":
                    await dtlsTransport.start(self.__remoteDtls[transceiver])
                    # await transceiver.receiver.receive(
                    #     self.__remoteRtp(transceiver)
                    # )
                if dtlsTransport.state == "connected":
                    if transceiver.currentDirection in ["sendonly", "sendrecv"]:
                        await transceiver.sender.send(self.__localRtp(transceiver))
                    if transceiver.currentDirection in ["recvonly", "sendrecv"]:
                        await transceiver.receiver.receive(
                            self.__remoteRtp(transceiver)
                        )

    async def __gather(self) -> None:
        coros = map(lambda t: t.iceGatherer.gather(), self.__iceTransports)
        await asyncio.gather(*coros)

    def __assertNotClosed(self) -> None:
        if self.__isClosed:
            raise InvalidStateError("RTCPeerConnection is closed")

    def __assertTrackHasNoSender(self, track: MediaStreamTrack) -> None:
        for sender in self.getSenders():
            if sender.track == track:
                raise InvalidAccessError("Track already has a sender")

    def __createDtlsTransport(self) -> RTCDtlsTransport:
        # create ICE transport
        iceGatherer = RTCIceGatherer(iceServers=self.__configuration.iceServers)
        iceGatherer.on("statechange", self.__updateIceGatheringState)
        iceTransport = RTCIceTransport(iceGatherer)
        iceTransport.on("statechange", self.__updateIceConnectionState)
        iceTransport.on("statechange", self.__updateConnectionState)
        self.__iceTransports.add(iceTransport)

        # create DTLS transport
        dtlsTransport = RTCDtlsTransport(iceTransport, self.__certificates)
        dtlsTransport.on("statechange", self.__updateConnectionState)
        self.__dtlsTransports.add(dtlsTransport)

        # update states
        self.__updateIceGatheringState()
        self.__updateIceConnectionState()
        self.__updateConnectionState()

        return dtlsTransport

    def __createTransceiver(
        self, direction: str, kind: str, sender_track=None
    ) -> RTCRtpTransceiver:
        dtlsTransport = self.__createDtlsTransport()
        transceiver = RTCRtpTransceiver(
            direction=direction,
            kind=kind,
            sender=RTCRtpSender(sender_track or kind, dtlsTransport),
            receiver=RTCRtpReceiver(kind, dtlsTransport),
        )
        transceiver.receiver._set_rtcp_ssrc(transceiver.sender._ssrc)
        transceiver.sender._stream_id = self.__stream_id
        transceiver._bundled = False
        transceiver._transport = dtlsTransport
        self.__transceivers.append(transceiver)
        return transceiver

    def __getTransceiverByMid(self, mid: str) -> Optional[RTCRtpTransceiver]:
        return next(filter(lambda x: x.mid == mid, self.__transceivers), None)

    def __getTransceiverByMLineIndex(self, index: int) -> Optional[RTCRtpTransceiver]:
        return next(
            filter(lambda x: x._get_mline_index() == index, self.__transceivers), None
        )

    def __localDescription(self) -> Optional[sdp.SessionDescription]:
        return self.__pendingLocalDescription or self.__currentLocalDescription

    def __localRtp(self, transceiver: RTCRtpTransceiver) -> RTCRtpSendParameters:
        rtp = RTCRtpSendParameters(
            codecs=transceiver._codecs,
            headerExtensions=transceiver._headerExtensions,
            muxId=transceiver.mid,
        )
        rtp.rtcp.cname = self.__cname
        rtp.rtcp.ssrc = transceiver.sender._ssrc
        rtp.rtcp.mux = True
        return rtp

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"RTCPeerConnection() {msg}", *args)

    def __remoteDescription(self) -> Optional[sdp.SessionDescription]:
        return self.__pendingRemoteDescription or self.__currentRemoteDescription

    def __remoteRtp(self, transceiver: RTCRtpTransceiver) -> RTCRtpReceiveParameters:
        media = self.__remoteDescription().media[transceiver._get_mline_index()]

        receiveParameters = RTCRtpReceiveParameters(
            codecs=transceiver._codecs,
            headerExtensions=transceiver._headerExtensions,
            muxId=media.rtp.muxId,
            rtcp=media.rtp.rtcp,
        )
        if len(media.ssrc):
            encodings: OrderedDict[int, RTCRtpDecodingParameters] = OrderedDict()
            for codec in transceiver._codecs:
                if is_rtx(codec):
                    if codec.parameters["apt"] in encodings and len(media.ssrc) == 2:
                        encodings[codec.parameters["apt"]].rtx = RTCRtpRtxParameters(
                            ssrc=media.ssrc[1].ssrc
                        )
                    continue

                encodings[codec.payloadType] = RTCRtpDecodingParameters(
                    ssrc=media.ssrc[0].ssrc, payloadType=codec.payloadType
                )
            receiveParameters.encodings = list(encodings.values())
        # NOTES: rtp接收路由设置, 路由关键字为ssrc或pt
        return receiveParameters

    def __setSignalingState(self, state: str) -> None:
        self.__signalingState = state
        self.emit("signalingstatechange")

    def __updateConnectionState(self) -> None:
        # compute new state
        # NOTE: we do not have a "disconnected" state
        dtlsStates = set(map(lambda x: x.state, self.__dtlsTransports))
        iceStates = set(map(lambda x: x.state, self.__iceTransports))
        if self.__isClosed:
            state = "closed"
        elif "failed" in iceStates or "failed" in dtlsStates:
            state = "failed"
        elif not iceStates.difference(["new", "closed"]) and not dtlsStates.difference(
            ["new", "closed"]
        ):
            state = "new"
        elif "checking" in iceStates or "connecting" in dtlsStates:
            state = "connecting"
        elif "new" in dtlsStates:
            # this avoids a spurious connecting -> connected -> connecting
            # transition after ICE connects but before DTLS starts
            state = "connecting"
        else:
            state = "connected"

        # update state
        if state != self.__connectionState:
            self.__log_debug("connectionState %s -> %s", self.__connectionState, state)
            self.__connectionState = state
            self.emit("connectionstatechange")

    def __updateIceConnectionState(self) -> None:
        # compute new state
        # NOTE: we do not have "connected" or "disconnected" states
        states = set(map(lambda x: x.state, self.__iceTransports))
        if self.__isClosed:
            state = "closed"
        elif "failed" in states:
            state = "failed"
        elif states == set(["completed"]):
            state = "completed"
        elif "checking" in states:
            state = "checking"
        else:
            state = "new"

        # update state
        if state != self.__iceConnectionState:
            self.__log_debug(
                "iceConnectionState %s -> %s", self.__iceConnectionState, state
            )
            self.__iceConnectionState = state
            self.emit("iceconnectionstatechange")

    def __updateIceGatheringState(self) -> None:
        # compute new state
        states = set(map(lambda x: x.iceGatherer.state, self.__iceTransports))
        if states == set(["completed"]):
            state = "complete"
        elif "gathering" in states:
            state = "gathering"
        else:
            state = "new"

        # update state
        if state != self.__iceGatheringState:
            self.__log_debug(
                "iceGatheringState %s -> %s", self.__iceGatheringState, state
            )
            self.__iceGatheringState = state
            self.emit("icegatheringstatechange")

    def __validate_description(
        self, description: sdp.SessionDescription, is_local: bool
    ) -> None:
        # check description is compatible with signaling state
        if is_local:
            if description.type == "offer":
                if self.signalingState not in ["stable", "have-local-offer"]:
                    raise InvalidStateError(
                        f'Cannot handle offer in signaling state "{self.signalingState}"'
                    )
            elif description.type == "answer":
                if self.signalingState not in [
                    "have-remote-offer",
                    "have-local-pranswer",
                ]:
                    raise InvalidStateError(
                        f'Cannot handle answer in signaling state "{self.signalingState}"'
                    )
        else:
            if description.type == "offer":
                if self.signalingState not in ["stable", "have-remote-offer"]:
                    raise InvalidStateError(
                        f'Cannot handle offer in signaling state "{self.signalingState}"'
                    )
            elif description.type == "answer":
                if self.signalingState not in [
                    "have-local-offer",
                    "have-remote-pranswer",
                ]:
                    raise InvalidStateError(
                        f'Cannot handle answer in signaling state "{self.signalingState}"'
                    )

        for media in description.media:
            # check ICE credentials were provided
            if not media.ice.usernameFragment or not media.ice.password:
                raise ValueError("ICE username fragment or password is missing")

            # check DTLS role is allowed
            if description.type == "offer" and media.dtls.role != "auto":
                raise ValueError("DTLS setup attribute must be 'actpass' for an offer")
            if description.type in ["answer", "pranswer"] and media.dtls.role not in [
                "client",
                "server",
            ]:
                raise ValueError(
                    "DTLS setup attribute must be 'active' or 'passive' for an answer"
                )

            # check RTCP mux is used
            if media.kind in ["audio", "video"] and not media.rtcp_mux:
                raise ValueError("RTCP mux is not enabled")

        # check the number of media section matches
        if description.type in ["answer", "pranswer"]:
            offer = (
                self.__remoteDescription() if is_local else self.__localDescription()
            )
            offer_media = [(media.kind, media.rtp.muxId) for media in offer.media]
            answer_media = [
                (media.kind, media.rtp.muxId) for media in description.media
            ]
            if answer_media != offer_media:
                raise ValueError("Media sections in answer do not match offer")
