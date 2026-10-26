import pytest

from tapo_web.crypto import Vault, VaultError


def test_vault_roundtrip():
    vault = Vault(Vault.generate_key())
    assert vault.decrypt(vault.encrypt("p@ss:w!rd")) == "p@ss:w!rd"


def test_vault_wrong_key_raises():
    token = Vault(Vault.generate_key()).encrypt("secret")
    with pytest.raises(VaultError):
        Vault(Vault.generate_key()).decrypt(token)


def test_vault_output_not_plaintext():
    vault = Vault(Vault.generate_key())
    assert "secret" not in vault.encrypt("secret")
    assert vault.encrypt("secret") != vault.encrypt("secret")  # random IV: equal inputs differ


def test_vault_rejects_garbage_token():
    with pytest.raises(VaultError):
        Vault(Vault.generate_key()).decrypt("not a token")
