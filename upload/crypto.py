"""Symmetric encryption for facility MySQL passwords at rest.

Facility credentials live in db.sqlite3, which is a plain file on the host. The
key is derived from FIELD_ENCRYPTION_KEY, falling back to SECRET_KEY so an
existing deployment keeps working — but note that rotating whichever value is in
use makes stored passwords undecryptable and the facilities must be re-entered.
"""
import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


@lru_cache(maxsize=4)
def _fernet_for(material):
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
    return Fernet(key)


def _fernet():
    material = settings.FIELD_ENCRYPTION_KEY or settings.SECRET_KEY
    if not material:
        raise ValueError('Neither FIELD_ENCRYPTION_KEY nor SECRET_KEY is set.')
    return _fernet_for(material)


def encrypt(plaintext):
    if not plaintext:
        return ''
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext):
    if not ciphertext:
        return ''
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError(
            'Stored credential could not be decrypted. FIELD_ENCRYPTION_KEY or '
            'SECRET_KEY has changed since it was saved; re-enter the password.'
        )
