"""Proxy Chat relay: runs on the game host's PC and sends the mod's events to the central server.

It reads game.log, keeps only the mod's own records, and sends them, signed with
a key that never leaves this PC. It opens no ports and accepts nothing inbound.
"""

__version__ = "0.1.0"
