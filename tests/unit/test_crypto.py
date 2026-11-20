import pytest

from curio.crypto import (
    KeyDecryptionError,
    KeyDecryptor,
    KeyEncryptor,
    PrivateKeyNotConfiguredError,
    PublicKeyNotConfiguredError,
    generate_keypair,
    hash_token,
    mask_last4,
)


def test_sealed_box_roundtrip_and_process_split():
    kp = generate_keypair()
    encryptor = KeyEncryptor(kp.public_key)
    assert not hasattr(encryptor, "decrypt")

    secret = "sk-test-super-secret-value-9876"
    ciphertext = encryptor.encrypt(secret)
    assert secret.encode() not in ciphertext

    decryptor = KeyDecryptor(kp.private_key)
    assert decryptor.decrypt(ciphertext) == secret


def test_decrypt_with_wrong_private_key_fails():
    kp1 = generate_keypair()
    kp2 = generate_keypair()

    ciphertext = KeyEncryptor(kp1.public_key).encrypt("AIza-secret-key")
    with pytest.raises(KeyDecryptionError):
        KeyDecryptor(kp2.private_key).decrypt(ciphertext)


def test_missing_keys_raise_clear_errors():
    with pytest.raises(PublicKeyNotConfiguredError):
        KeyEncryptor("")
    with pytest.raises(PrivateKeyNotConfiguredError):
        KeyDecryptor(None)
    with pytest.raises(PrivateKeyNotConfiguredError):
        KeyDecryptor("")


def test_token_hashing_and_masking():
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")
    assert len(hash_token("abc")) == 64
    assert mask_last4("sk-1234567890abcd") == "abcd"
    assert mask_last4("  xy  ") == "xy"
