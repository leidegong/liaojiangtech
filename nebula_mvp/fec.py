"""Systematic Reed-Solomon erasure code over GF(2^8) for video fragments.

k data blocks are sent unchanged, followed by m parity blocks; any k of the
k + m blocks rebuild the frame. Parity rows form a Cauchy matrix, so every
square submatrix is invertible and any mix of lost blocks up to m is solvable.
"""
from functools import lru_cache
import math
import numpy as np

MAX_BLOCKS = 255        # Data plus parity blocks per frame, bounded by the field size.
SURVIVAL_TARGET = 0.99  # Chance a protected frame stays decodable under i.i.d. loss.

_EXP = np.zeros(512, dtype=np.int32)
_LOG = np.zeros(256, dtype=np.int32)
_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D  # x^8 + x^4 + x^3 + x^2 + 1
_EXP[255:510] = _EXP[:255]
MUL = np.zeros((256, 256), dtype=np.uint8)  # MUL[a, b] = a * b
MUL[1:, 1:] = _EXP[_LOG[1:, None] + _LOG[None, 1:]]
INV = np.zeros(256, dtype=np.uint8)
INV[1:] = _EXP[255 - _LOG[1:]]


def _coefficients(parity_rows, data_columns):
    """Cauchy entries 1 / (x_j + y_i) with x_j = 255 - j and y_i = i."""
    rows = 255 - np.asarray(parity_rows, dtype=np.int32)
    return INV[rows[:, None] ^ np.asarray(data_columns, dtype=np.int32)[None, :]]


def _matrix(blocks, size):
    """Blocks as a zero-padded (len(blocks), size) uint8 array."""
    out = np.zeros((len(blocks), size), dtype=np.uint8)
    for row, block in enumerate(blocks):
        out[row, :len(block)] = np.frombuffer(block, dtype=np.uint8)
    return out


def _combine(coefficients, data):
    """Sum over rows of coefficient * row, i.e. one GF(2^8) linear combination."""
    return np.bitwise_xor.reduce(MUL[coefficients[:, None], data], axis=0)


def parity(blocks, count):
    """`count` parity blocks for data blocks; a shorter last block is zero-padded."""
    data = _matrix(blocks, len(blocks[0]))
    coefficients = _coefficients(range(count), range(len(blocks)))
    return [_combine(coefficients[j], data).tobytes() for j in range(count)]


def _solve(matrix, rows):
    """Solve matrix . x = rows over GF(2^8) by Gauss-Jordan elimination."""
    size = len(matrix)
    for col in range(size):
        pivot = next(r for r in range(col, size) if matrix[r][col])
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = int(INV[matrix[col][col]])
        matrix[col] = [int(MUL[scale, v]) for v in matrix[col]]
        rows[col] = MUL[scale, rows[col]]
        for r in range(size):
            factor = matrix[r][col]
            if r != col and factor:
                matrix[r] = [a ^ int(MUL[factor, b]) for a, b in zip(matrix[r], matrix[col])]
                rows[r] = rows[r] ^ MUL[factor, rows[col]]
    return rows


def reassemble(frame_bytes, data_count, parts):
    """Rebuild a frame from any `data_count` of its blocks ({index: bytes}).

    Indices below data_count are data blocks, the rest parity. Only the missing
    data blocks are solved for: the known data is removed from each parity block,
    leaving a small system in the missing blocks alone.
    """
    missing = [i for i in range(data_count) if i not in parts]
    blocks = dict(parts)
    if missing:
        rows = sorted(i for i in parts if i >= data_count)[:len(missing)]
        if len(rows) < len(missing):
            raise ValueError("Not enough fragments to rebuild the frame")
        present = [i for i in range(data_count) if i in parts]
        size = len(parts[rows[0]])
        data = _matrix([parts[i] for i in present], size)
        syndromes = []
        for row in rows:
            block = np.frombuffer(parts[row], dtype=np.uint8).copy()
            if present:
                block ^= _combine(_coefficients([row - data_count], present)[0], data)
            syndromes.append(block)
        matrix = _coefficients([row - data_count for row in rows], missing).tolist()
        for index, block in zip(missing, _solve(matrix, syndromes)):
            blocks[index] = block.tobytes()
    return b"".join(blocks[i] for i in range(data_count))[:frame_bytes]


@lru_cache(maxsize=4096)
def _parity_for(data_count, loss_permille):
    loss = loss_permille / 1000
    limit = min(data_count, MAX_BLOCKS - data_count)
    for count in range(limit + 1):
        total = data_count + count
        survive = sum(math.comb(total, lost) * loss ** lost * (1 - loss) ** (total - lost)
                      for lost in range(count + 1))
        if survive >= SURVIVAL_TARGET:
            return count
    return limit


def interleave_depth(burst):
    """How many full-resolution frames to mix on the wire.

    Independent loss has mean run 1/(1-p) ≈ 1.05 at 5%, so values below 1.5
    stay depth 1 (no extra delay). Moderate runs mix two frames; mean runs of
    6 or more mix three, which is one extra capture of hold (~70 ms more at
    15 FPS) but halves the hits of a long packet-run again. Deeper than 3
    would hold past the 250 ms video budget.
    """
    if not burst or burst < 1.5:
        return 1
    if burst < 6:
        return 2
    return 3


def _run_quantile(burst, tail=0.05):
    """Smallest run length k with P(L > k) ≈ tail for a geometric mean `burst`."""
    stay = 1 - 1 / burst
    if stay <= 0:
        return max(1, math.ceil(burst))
    return max(1, math.ceil(math.log(tail) / math.log(stay)))


def parity_count(data_count, loss, burst=None):
    """Fewest parity blocks so a frame of `data_count` blocks survives the
    modelled loss, at most 100% overhead.

    `loss` is treated as i.i.d. (quantized to 0.1% so budgets and packetization
    agree). `burst` is the mean run of consecutive losses: after mixing
    `interleave_depth(burst)` frames, a geometric 95th-percentile run hits
    about that many / depth fragments of one codeword. The two requirements
    are combined; i.i.d. with no burst is unchanged.
    """
    if not 0 < data_count < MAX_BLOCKS:
        return 0
    permille = round(loss * 1000) if loss else 0
    iid = _parity_for(data_count, min(permille, 1000)) if permille > 0 else 0
    extra = 0
    if burst and burst >= 1.5:
        extra = math.ceil(_run_quantile(burst) / interleave_depth(burst))
    limit = min(data_count, MAX_BLOCKS - data_count)
    return min(limit, max(iid, extra))
