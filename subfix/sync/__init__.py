"""Subtitle synchronisation algorithms.

* :mod:`.offset`    – one constant shift (FFT cross-correlation of speech activity)
* :mod:`.drift`     – linear scale + offset (frame-rate mismatch)
* :mod:`.piecewise` – different offsets per section (cuts / inserted scenes), Viterbi over cues
* :mod:`.smart`     – align subtitle *text* to a Whisper transcript
"""

from .common import SyncResult

__all__ = ["SyncResult"]
