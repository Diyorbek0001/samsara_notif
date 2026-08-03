from cryptography.fernet import Fernet

from app.security import TokenCipher


def test_token_cipher_round_trip():
    cipher = TokenCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt("samsara-secret")
    assert encrypted != "samsara-secret"
    assert cipher.decrypt(encrypted) == "samsara-secret"
