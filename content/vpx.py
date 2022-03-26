from struct import pack, unpack_from, unpack
from typing import List, Tuple, Type, TypeVar

DESCRIPTOR_T = TypeVar("DESCRIPTOR_T")


class Vp8PayloadDescriptor:
    def __init__(
        self,
        partition_start,
        partition_id,
        picture_id=None,
        tl0picidx=None,
        tid=None,
        keyidx=None,
    ) -> None:
        self.partition_start = partition_start
        self.partition_id = partition_id
        self.picture_id = picture_id
        self.tl0picidx = tl0picidx
        self.tid = tid
        self.keyidx = keyidx

    def __repr__(self) -> str:
        return (
            f"VpxPayloadDescriptor(S={self.partition_start}, "
            f"PID={self.partition_id}, pic_id={self.picture_id})"
        )

    @classmethod
    def parse(cls: Type[DESCRIPTOR_T], data: bytes) -> Tuple[DESCRIPTOR_T, bytes]:
        if len(data) < 1:
            raise ValueError("VPX descriptor is too short")

        # first byte
        octet = data[0]
        extended = octet >> 7
        partition_start = (octet >> 4) & 1
        partition_id = octet & 0xF
        picture_id = None
        tl0picidx = None
        tid = None
        keyidx = None
        pos = 1

        # extended control bits
        if extended:
            if len(data) < pos + 1:
                raise ValueError("VPX descriptor has truncated extended bits")

            octet = data[pos]
            ext_I = (octet >> 7) & 1
            ext_L = (octet >> 6) & 1
            ext_T = (octet >> 5) & 1
            ext_K = (octet >> 4) & 1
            pos += 1

            # picture id
            if ext_I:
                if len(data) < pos + 1:
                    raise ValueError("VPX descriptor has truncated PictureID")

                if data[pos] & 0x80:
                    if len(data) < pos + 2:
                        raise ValueError("VPX descriptor has truncated long PictureID")

                    picture_id = unpack_from("!H", data, pos)[0] & 0x7FFF
                    pos += 2
                else:
                    picture_id = data[pos]
                    pos += 1

            # unused
            if ext_L:
                if len(data) < pos + 1:
                    raise ValueError("VPX descriptor has truncated TL0PICIDX")

                tl0picidx = data[pos]
                pos += 1
            if ext_T or ext_K:
                if len(data) < pos + 1:
                    raise ValueError("VPX descriptor has truncated T/K")

                t_k = data[pos]
                if ext_T:
                    tid = ((t_k >> 6) & 3, (t_k >> 5) & 1)
                if ext_K:
                    keyidx = t_k & 0x1F
                pos += 1

        obj = cls(
            partition_start=partition_start,
            partition_id=partition_id,
            picture_id=picture_id,
            tl0picidx=tl0picidx,
            tid=tid,
            keyidx=keyidx,
        )
        return obj, data[pos:]


class Vp9PayloadDescriptor:
    def __init__(self, picture_id=None, tid=None, sid=None, keyframe=None):
        self.picture_id = picture_id
        self.tid = tid
        self.sid = sid
        self.keyframe = keyframe

    @classmethod
    def parse(cls: Type[DESCRIPTOR_T], data: bytes) -> DESCRIPTOR_T:
        if len(data) < 1:
            raise ValueError("VP9 descriptor has truncated extended bits")

        picture_id = None
        tid = None
        sid = None
        keyframe = None
        offset = 0
        ei = data[0] >> 7 & 1
        ep = data[0] >> 6 & 1
        el = data[0] >> 5 & 1
        ef = data[0] >> 4 & 1
        eb = data[0] >> 3 & 1
        ee = data[0] >> 2 & 1
        ev = data[0] >> 1 & 1

        offset += 1
        if ei:
            if len(data) < offset:
                raise ValueError("VP9 descriptor has truncated extended bits")
            em = data[offset] >> 7 & 1
            high_bytes = data[offset] & 0x7F
            if em:
                offset += 1
                picture_id = (high_bytes << 8) + data[offset]
            else:
                picture_id = high_bytes

        offset += 1
        if el:
            sid = data[offset] >> 1 & 0x07
            tid = data[offset] >> 5 & 0x07

        keyframe = ((not ep) and eb and (sid is None or sid == 0))

        return cls(picture_id=picture_id,
                   tid=tid,
                   sid=sid,
                   keyframe=keyframe)
