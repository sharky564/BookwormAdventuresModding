"""Extract the empirical tile-spawn distribution from .bwa save(s).

NumLetter* in a save is a lifetime histogram of tiles that have SPAWNED on the
rack (NOT letters used -- confirmed: their sum is ~1.9x TotalLettersSpelled).
It is therefore an empirical sample of the game's refill distribution, usable
to validate or replace the solver's hardcoded distribution() weights.

Pass several completed saves to pool a larger sample.

Usage:
    python3 bwa_letterdist.py save1.bwa [save2.bwa ...]
"""

import struct, sys

# Solver's current hardcoded distribution (constants.hpp), A..Z.
SOLVER_DIST = [
    0.0932,
    0.0171,
    0.0218,
    0.0376,
    0.13,
    0.0235,
    0.0257,
    0.0252,
    0.0723,
    0.0077,
    0.0056,
    0.0466,
    0.0214,
    0.0547,
    0.0663,
    0.0261,
    0.0115,
    0.0752,
    0.0594,
    0.0684,
    0.0428,
    0.0171,
    0.0154,
    0.006,
    0.0252,
    0.0042,
]


def counts(path):
    data = open(path, "rb").read()

    def ni(name):
        p = data.find(struct.pack("<H", len(name)) + name.encode())
        if p < 0:
            return 0
        return struct.unpack_from("<I", data, p + 2 + len(name) + 4)[0]

    return {chr(c): ni(f"NumLetter{chr(c)}") for c in range(65, 91)}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    pooled = {chr(c): 0 for c in range(65, 91)}
    for p in sys.argv[1:]:
        for k, v in counts(p).items():
            pooled[k] += v
    total = sum(pooled.values())
    if total == 0:
        print("No NumLetter* data found.")
        return
    print(f"# Pooled spawn sample: {total} tiles from {len(sys.argv) - 1} save(s)\n")
    print(f"{'L':3}{'count':>8}{'observed':>11}{'solver':>9}{'delta':>9}")
    for k in range(26):
        c = chr(65 + k)
        of = pooled[c] / total
        d = of - SOLVER_DIST[k]
        print(f"  {c}{pooled[c]:8d}{of:11.4f}{SOLVER_DIST[k]:9.4f}{d:+9.4f}")
    # Emit a ready-to-paste C++ initializer of the empirical distribution.
    print("\n// Empirical distribution (paste into constants.hpp distribution()):")
    vals = ", ".join(f"{pooled[chr(65 + k)] / total:.4f}" for k in range(26))
    print(f"static thread_local std::discrete_distribution<> d{{\n    {vals}\n}};")


if __name__ == "__main__":
    main()
