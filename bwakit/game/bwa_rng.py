"""Reproduction of Bookworm Adventures Deluxe's RNG.

Recovered by disassembling the engine: the game uses the standard MSVCRT
`rand()` / `srand()`, exposed to Lua as math.random / math.randomseed, and the
custom QRand:Next() is a weighted wrapper over math.random. No script ever calls
math.randomseed, so the C seed keeps its default value of 1 and the entire
randomness stream is a fixed, reproducible sequence from process start.

MSVCRT generator (verified from the exe at 0x4e801e):
    state = (state * 214013 + 2531011) & 0xFFFFFFFF
    rand() = (state >> 16) & 0x7FFF          # 0..32767

Lua 5.1 math.random maps that to its results:
    math.random()        -> rand()/(RAND_MAX+1)             in [0,1)
    math.random(m)       -> floor(r01*m)+1                  in 1..m
    math.random(m,n)     -> floor(r01*(n-m+1))+m            in m..n
(Lua uses r01 = rand()/(RAND_MAX+1) with RAND_MAX = 32767.)
"""

RAND_MAX = 0x7FFF


class MsvcRng:
    def __init__(self, seed=1):
        self.state = seed & 0xFFFFFFFF

    def srand(self, seed):
        self.state = seed & 0xFFFFFFFF

    def rand(self):
        self.state = (self.state * 214013 + 2531011) & 0xFFFFFFFF
        return (self.state >> 16) & 0x7FFF

    # Lua 5.1 math.random semantics
    def random(self, m=None, n=None):
        r01 = self.rand() / (RAND_MAX + 1)
        if m is None:
            return r01
        if n is None:
            return int(r01 * m) + 1
        return int(r01 * (n - m + 1)) + m


if __name__ == "__main__":
    # The canonical MSVCRT rand() sequence for seed=1 is well documented; print
    # the first values so the stream can be checked against the game.
    r = MsvcRng(1)
    first = [r.rand() for _ in range(10)]
    print("MSVCRT rand() seed=1, first 10:", first)
    # Known-correct reference values for MSVCRT seed=1:
    expected = [41, 18467, 6334, 26500, 19169, 15724, 11478, 29358, 26962, 24464]
    print("expected (reference)        :", expected)
    print("MATCH" if first == expected else "MISMATCH")
