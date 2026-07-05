import struct, glob, os


def plen(f):
    return {0x00: 4, 0x20: 8, 0x40: 6, 0x60: 10}.get(f & 0xF0)


FRAME_LABELS = {
    "intro",
    "engage",
    "idle",
    "flinch",
    "death",
    "slide",
    "powerup",
    "walk",
    "stunned",
    "effect",
    "effectloop",
    "win",
    "taunt",
    "causeDamage",
}


def is_fl(s):
    return s in FRAME_LABELS or s.startswith("attack") or s.startswith("cause")


def header_end(data):
    N = len(data)
    o = 9
    o += 8
    nimg = struct.unpack_from("<H", data, o - 2)[0]  # nimg read at o-2? fix:
    # re-read cleanly
    o = 9 + 4 + 2 + 2
    nimg = struct.unpack_from("<H", data, o)[0]
    o += 2
    for k in range(nimg):
        n = struct.unpack_from("<H", data, o)[0]
        o += 2 + n + 6
    return o


def find_frame0(data, off):
    """frame0 = a place-burst whose run length == the u16 header count right before it."""
    N = len(data)
    i = off
    best = None
    while i < N - 3:
        if data[i] == 0x80:
            j = i
            n = 0
            while j < N - 2 and data[j] == 0x80:
                j += 3
                n += 1
            if i >= 5:
                hdr = struct.unpack_from("<H", data, i - 2)[0]
                if hdr == n and n >= 2:
                    return i  # first burst matching its header count
            i = j
        else:
            i += 1
    return None


def parse_pam(path):
    data = open(path, "rb").read()
    N = len(data)
    fps = data[8]
    off = header_end(data)
    pos = find_frame0(data, off)
    if pos is None:
        return fps, 0, {}, N, N

    def lbl(p):
        if p + 2 > N:
            return None
        ln = struct.unpack_from("<H", data, p)[0]
        if 1 <= ln <= 20 and p + 2 + ln <= N:
            try:
                s = data[p + 2 : p + 2 + ln].decode("ascii")
            except:
                return None
            if all(32 <= ord(c) < 127 for c in s):
                return s
        return None

    def ptrans(j, cnt):
        for k in range(cnt):
            if j >= N:
                return None
            f = data[j]
            pl = plen(f)
            if pl is None:
                return None
            j += 1 + pl + (0 if k == cnt - 1 else 1)
        return j

    i = pos - 3  # at the flags+count header of frame 0
    frame = 0
    labels = {}
    safety = 0
    while i < N - 2 and safety < 60000:
        safety += 1
        s = lbl(i)
        if s is not None:
            if is_fl(s):
                labels.setdefault(s, frame)
            i += 2 + struct.unpack_from("<H", data, i)[0]
            v = lbl(i)
            if (
                v is not None
                and (" " in v or any(c.isdigit() for c in v))
                and not is_fl(v)
            ):
                i += 2 + struct.unpack_from("<H", data, i)[0]
            continue
        if i + 3 > N:
            break
        cnt = struct.unpack_from("<H", data, i + 1)[0]
        if 1 <= cnt <= 80:
            j = i + 3
            if data[j] == 0x80:
                np = 0
                while np < cnt and data[j] == 0x80:
                    j += 3
                    np += 1
                if j < N and data[j] == 0x00:
                    j += 1
            nj = ptrans(j, cnt)
            if nj is not None:
                i = nj
                frame += 1
                continue
        i += 1
    return fps, frame, labels, i, N


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        fps, total, labels, end, N = parse_pam(path)
        nm = os.path.basename(path).replace(".pam", "")
        st = "EOF" if end >= N - 3 else f"0x{end:x}/0x{N:x}"
        order = sorted(labels.items(), key=lambda x: x[1])
        print(
            f"{nm} fps={fps} frames~{total} [{st}]: "
            + ", ".join(f"{k}={v}" for k, v in order)
        )
