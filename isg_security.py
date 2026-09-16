import hashlib
import hmac
import os
import secrets
import string


PASSWORD_POLICY_MESSAGE = (
    "Parola en az 8 karakter olmalı; en az bir küçük harf, "
    "bir büyük harf, bir rakam ve bir özel karakter içermelidir."
)


def validate_password(password):
    is_valid = all(
        (
            len(password) >= 8,
            any(character.islower() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(character in string.punctuation for character in password),
        )
    )

    return is_valid, None if is_valid else PASSWORD_POLICY_MESSAGE


def get_required_secret(variable_name, minimum_length=32):
    value = os.environ.get(variable_name, "")

    if len(value) < minimum_length:
        raise RuntimeError(
            f"{variable_name} ortam değişkeni en az "
            f"{minimum_length} karakter uzunluğunda tanımlanmalıdır."
        )

    return value


def get_ai_api_key(required=False):
    api_key = os.environ.get("ISG_AI_API_KEY", "")

    if required and len(api_key) < 32:
        raise RuntimeError(
            "ISG_AI_API_KEY ortam değişkeni en az 32 karakter "
            "uzunluğunda tanımlanmalıdır."
        )

    return api_key


def verify_ai_api_key(candidate):
    configured_key = get_ai_api_key(required=False)

    if len(configured_key) < 32 or not candidate:
        return False

    return hmac.compare_digest(configured_key, candidate)


def generate_csrf_token(session):
    token = session.get("_csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token

    return token


def verify_csrf_token(session, candidate):
    expected = session.get("_csrf_token", "")
    return bool(
        expected
        and candidate
        and hmac.compare_digest(expected, candidate)
    )


def login_attempt_key(ip_address, username):
    value = f"{ip_address}|{username.casefold()}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()
