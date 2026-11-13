"""Where the relay keeps its private key: Windows Credential Manager, through keyring.

The key is never written to a file, logged or printed. Tests swap in MemoryKeyStore.
"""

from proxychat_core import signing

SERVICE = "proxychat-relay"


class KeyringStore:
    def __init__(self, service=SERVICE):
        import keyring
        self.keyring = keyring
        self.service = service

    def put(self, key_id, private_key):
        self.keyring.set_password(self.service, key_id, signing.b64e(signing.private_bytes(private_key)))

    def get(self, key_id):
        stored = self.keyring.get_password(self.service, key_id)
        return signing.private_from_bytes(signing.b64d(stored)) if stored else None

    def delete(self, key_id):
        try:
            self.keyring.delete_password(self.service, key_id)
        except self.keyring.errors.PasswordDeleteError:
            pass


class MemoryKeyStore:
    def __init__(self):
        self.keys = {}

    def put(self, key_id, private_key):
        self.keys[key_id] = signing.private_bytes(private_key)

    def get(self, key_id):
        raw = self.keys.get(key_id)
        return signing.private_from_bytes(raw) if raw else None

    def delete(self, key_id):
        self.keys.pop(key_id, None)
