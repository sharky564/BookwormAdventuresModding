"""Full field dumper for Bookworm Adventures .bwa saves.

Validated encoding (confirmed across fresh / mid / completed saves):
  field := <u16 namelen><name><u32 type><value>
    type 0 (int)    : <u32 value>                 (immediately after the tag)
    type 1 (string) : <u16 len><ascii>            (floats are stored as ascii)
    type 2 (bool)   : <u8 value>
    type 4 (group)  : a Torque container; children follow as further fields

This flat scanner finds every scalar field anywhere in the file (including
inside groups) and prints name = value. Group containers are marked with ':'.
The repetitive PAM/animation/render internals can be collapsed with --clean.

Usage:
    python3 bwa_dump.py save.bwa [--clean] [--raw]
"""

from __future__ import annotations
import struct, sys

PAM_NOISE = {
    "mPAM",
    "mFade",
    "mTempYOff",
    "mKeyAnim",
    "mDone",
    "mAnimId",
    "mXOff",
    "mYOff",
    "mFadeDoneUpdating",
    "mRate",
    "mMessageMap",
    "IS_PAM_ANIM",
    "mTempXOff",
    "mPriority",
    "mTarget",
    "mColor",
    "mFlag",
    "mSlot",
    "a",
    "b",
    "g",
    "r",
    "mChanceIfDupes",
    "mRemove",
    "mChance",
    "mUpdate",
    "mMaxDupes",
    "mHidden",
    "mClassName",
    "mAttributes",
    "mPAMName",
    "mNumFrames",
    "mFPS",
    "mLoop",
    "mStartFrame",
    "mCurFrame",
    "mElapsed",
    "mPlaying",
    "mName",
}


def decode_fields(data: bytes):
    n = len(data)
    u16 = lambda o: struct.unpack_from("<H", data, o)[0]
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    out = []
    i = 0
    while i < n - 6:
        ln = u16(i)
        if 1 <= ln <= 48 and i + 2 + ln + 4 <= n:
            name = data[i + 2 : i + 2 + ln]
            if all(65 <= b <= 90 or 97 <= b <= 122 or b in (32, 95) for b in name):
                typ = u32(i + 2 + ln)
                vo = i + 2 + ln + 4
                nm = name.decode()
                if typ == 0 and vo + 4 <= n:
                    out.append((nm, "int", u32(vo)))
                    i = vo + 4
                    continue
                if typ == 1 and vo + 2 <= n:
                    sl = u16(vo)
                    if vo + 2 + sl <= n and sl <= 255:
                        out.append(
                            (
                                nm,
                                "str",
                                data[vo + 2 : vo + 2 + sl].decode("ascii", "replace"),
                            )
                        )
                        i = vo + 2 + sl
                        continue
                if typ == 2 and vo + 1 <= n:
                    out.append((nm, "bool", bool(data[vo])))
                    i = vo + 1
                    continue
                if typ == 4:
                    out.append((nm, "group", ""))
                    i = vo
                    continue
        i += 1
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    clean = "--clean" in sys.argv
    data = open(sys.argv[1], "rb").read()
    fields = decode_fields(data)
    print(f"# {sys.argv[1]}: {len(fields)} fields, {len(data)} bytes\n")
    in_noise = False
    for nm, t, v in fields:
        if clean and nm in PAM_NOISE:
            if not in_noise:
                print("    ... [render/animation/flag internals collapsed] ...")
                in_noise = True
            continue
        in_noise = False
        if t == "str":
            print(f'{nm} = "{v}"')
        elif t == "group":
            print(f"{nm}:")
        else:
            print(f"{nm} = {v}")


if __name__ == "__main__":
    main()
