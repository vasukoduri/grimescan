"""Fixed-memory data structures, so a scan's memory use doesn't grow with the dataset.

* :class:`HyperLogLog`: approximate distinct counts (~1% error at the default precision)
* :class:`Reservoir`: a uniform random sample of fixed size
* :class:`TopK`: approximate most-frequent values (Space-Saving algorithm)
* :class:`BloomFilter`: "have I possibly seen this before?", for duplicate candidates
* :class:`RunningStats`: count, mean, variance, min, max in one pass
* :class:`MinHasher`: MinHash signatures and LSH buckets for near-duplicate detection
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Hashable, Iterable
from typing import Any

import numpy as np

_MASK64 = (1 << 64) - 1


def hash64(value: str | bytes) -> int:
    """Fast, stable 64-bit hash (blake2b), independent of Python's per-process hash seed."""
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogatepass")
    return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), "little")


class HyperLogLog:
    def __init__(self, precision: int = 12):
        self.p = precision
        self.m = 1 << precision
        self.registers = bytearray(self.m)
        self._alpha = 0.7213 / (1 + 1.079 / self.m)
        self._exact: set[int] | None = set()  # exact while small

    def add(self, value: str) -> None:
        h = hash64(value)
        if self._exact is not None:
            self._exact.add(h)
            if len(self._exact) > 2048:
                for x in self._exact:
                    self._add_hash(x)
                self._exact = None
            return
        self._add_hash(h)

    def _add_hash(self, h: int) -> None:
        idx = h >> (64 - self.p)
        w = (h << self.p) & _MASK64  # the remaining bits, moved to the top
        rank = 64 - w.bit_length() + 1 if w else (64 - self.p) + 1  # leading zeros + 1
        if rank > self.registers[idx]:
            self.registers[idx] = rank

    def count(self) -> int:
        if self._exact is not None:
            return len(self._exact)
        regs = np.frombuffer(bytes(self.registers), dtype=np.uint8).astype(np.float64)
        est = self._alpha * self.m * self.m / float(np.sum(np.power(2.0, -regs)))
        zeros = int(np.count_nonzero(regs == 0))
        if est <= 2.5 * self.m and zeros:
            est = self.m * math.log(self.m / zeros)
        return int(round(est))


class Reservoir:
    """Uniform sample of up to ``size`` items (Algorithm R), with each item's original index."""

    def __init__(self, size: int = 10_000, seed: int = 0):
        self.size = size
        self.items: list[Any] = []
        self.indices: list[int] = []
        self.seen = 0
        self._rng = random.Random(seed)
        self._w = 1.0
        self._next = 0

    def add(self, item: Any, index: int | None = None) -> None:
        # Algorithm L: draws random numbers only when an item is actually kept
        self.seen += 1
        idx = self.seen - 1 if index is None else index
        if len(self.items) < self.size:
            self.items.append(item)
            self.indices.append(idx)
            if len(self.items) == self.size:
                self._advance()
            return
        if self.seen == self._next:
            j = self._rng.randrange(self.size)
            self.items[j] = item
            self.indices[j] = idx
            self._advance()

    def _advance(self) -> None:
        r = self._rng.random
        self._w *= math.exp(math.log(r() or 1e-12) / self.size)
        self._next = (
            self.seen + int(math.log(r() or 1e-12) / math.log(1 - self._w)) + 1 if self._w < 1 else self.seen + 1
        )


class TopK:
    """Approximate heavy hitters with the Space-Saving algorithm (exact while under capacity)."""

    def __init__(self, capacity: int = 200):
        self.capacity = capacity
        self.counts: dict[Hashable, int] = {}
        self.errors: dict[Hashable, int] = {}
        self.total = 0

    def add(self, value: Hashable, n: int = 1) -> None:
        self.total += n
        if value in self.counts:
            self.counts[value] += n
            return
        if len(self.counts) < self.capacity:
            self.counts[value] = n
            self.errors[value] = 0
            return
        victim = min(self.counts, key=self.counts.__getitem__)
        floor = self.counts.pop(victim)
        self.errors.pop(victim, None)
        self.counts[value] = floor + n
        self.errors[value] = floor

    def most_common(self, k: int = 10) -> list[tuple[Hashable, int]]:
        return sorted(self.counts.items(), key=lambda kv: -kv[1])[:k]

    @property
    def exact(self) -> bool:
        return not any(self.errors.values())


class BloomFilter:
    def __init__(self, capacity: int = 1_000_000, error_rate: float = 0.001):
        capacity = max(capacity, 1000)
        self.bits = int(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.k = max(1, int(round(self.bits / capacity * math.log(2))))
        self.array = bytearray((self.bits + 7) // 8)

    def _positions(self, h: int) -> Iterable[int]:
        h1, h2 = h & 0xFFFFFFFF, (h >> 32) | 1
        for i in range(self.k):
            yield (h1 + i * h2) % self.bits

    def add_and_check(self, h: int) -> bool:
        """Add a 64-bit hash; return True if it was (probably) already present."""
        present = True
        for pos in self._positions(h):
            byte, bit = divmod(pos, 8)
            if not self.array[byte] & (1 << bit):
                present = False
                self.array[byte] |= 1 << bit
        return present


class RunningStats:
    """Welford's online algorithm."""

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0
        self.min = math.inf
        self.max = -math.inf

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self._m2 += d * (x - self.mean)
        if x < self.min:
            self.min = x
        if x > self.max:
            self.max = x

    @property
    def std(self) -> float:
        return math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else 0.0


class MinHasher:
    """MinHash over token shingles, with banded LSH to find near-duplicate candidates."""

    _PRIME = (1 << 61) - 1

    def __init__(self, num_perm: int = 64, bands: int = 8, seed: int = 1):
        if num_perm % bands:
            raise ValueError("num_perm must be divisible by bands")
        rng = np.random.default_rng(seed)
        self.num_perm = num_perm
        self.bands = bands
        self.rows = num_perm // bands
        self.a = rng.integers(1, self._PRIME, num_perm, dtype=np.uint64)
        self.b = rng.integers(0, self._PRIME, num_perm, dtype=np.uint64)

    @staticmethod
    def shingles(text: str, n: int = 3) -> set[str]:
        t = " ".join(text.lower().split())
        if len(t) <= n:
            return {t} if t else set()
        return {t[i : i + n] for i in range(len(t) - n + 1)}

    def signature(self, tokens: set[str]) -> np.ndarray:
        if not tokens:
            return np.full(self.num_perm, np.iinfo(np.uint64).max, dtype=np.uint64)
        # Python's built-in string hash is fast and stable within one process, which is all MinHash needs here
        hv = np.fromiter((hash(t) & 0x1FFFFFFFFFFFFFFF for t in tokens), dtype=np.uint64, count=len(tokens))
        # (a*h + b) mod p, computed in uint64 with wraparound; good enough for MinHash ordering
        phv = (np.outer(hv, self.a) + self.b) % np.uint64(self._PRIME)
        return phv.min(axis=0)

    def band_keys(self, sig: np.ndarray) -> list[tuple[int, bytes]]:
        return [(i, sig[i * self.rows : (i + 1) * self.rows].tobytes()) for i in range(self.bands)]

    @staticmethod
    def similarity(a: np.ndarray, b: np.ndarray) -> float:
        return np.count_nonzero(a == b) / len(a)
