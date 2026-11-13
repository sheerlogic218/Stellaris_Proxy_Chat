"""Incremental reader for game.log, which Stellaris truncates on every launch."""

import os
from pathlib import Path


class LogTailer:
    def __init__(self, path):
        self.path = Path(path)
        self._pos = 0
        self._ino = None
        self._buf = ""

    def poll(self):
        """Return complete lines written since the last call ([] if none or missing).

        Starts from the top of the file, so the first poll replays the current
        launch's log. A shrunk or replaced file means the game was relaunched.
        """
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        if st.st_size < self._pos or (self._ino is not None and st.st_ino != self._ino):
            self._pos, self._buf = 0, ""
        self._ino = st.st_ino
        if st.st_size == self._pos:
            return []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._pos)
            self._buf += f.read()
            self._pos = f.tell()
        *lines, self._buf = self._buf.split("\n")
        return lines
