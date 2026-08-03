from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored Samsara token cannot be decrypted") from exc
