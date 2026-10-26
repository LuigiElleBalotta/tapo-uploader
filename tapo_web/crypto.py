"""Encryption of the secrets kept in the database (camera passwords, YouTube tokens)."""
from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class Vault:
    """Fernet encryption with a key from the environment. A wrong key or a damaged token raises VaultError."""

    def __init__(self, key):
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as err:
            raise VaultError("the encryption key is not a valid Fernet key") from err

    @staticmethod
    def generate_key():
        return Fernet.generate_key().decode()

    def encrypt(self, text):
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token):
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, TypeError) as err:
            raise VaultError("cannot decrypt: wrong key or damaged data") from err
