"""
Windows DPAPI encryption helpers for NVSky.

Used to store the Bluesky App Password (and the DB master key) so that
the encrypted value:
- cannot be decrypted on another machine
- cannot be decrypted by another Windows user account on the same machine

This uses ctypes directly against crypt32.dll / kernel32.dll so it has
no dependency beyond the Python standard library.

argtypes/restype are set explicitly on every windll function used here:
on 64-bit Python, ctypes falls back to assuming c_int (32-bit) for any
unset arg/return type, which can truncate a 64-bit pointer and cause an
intermittent access violation. CRYPTPROTECT_UI_FORBIDDEN is passed on
every call so a broken user profile/keystore fails with an exception we
can catch, instead of popping a Windows UI prompt.
"""

import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_CryptProtectData = ctypes.windll.crypt32.CryptProtectData
_CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(DATA_BLOB),
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_CryptProtectData.restype = wintypes.BOOL

_CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
_CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    ctypes.POINTER(wintypes.LPCWSTR),
    ctypes.POINTER(DATA_BLOB),
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_CryptUnprotectData.restype = wintypes.BOOL

_LocalFree = ctypes.windll.kernel32.LocalFree
_LocalFree.argtypes = [wintypes.HLOCAL]
_LocalFree.restype = wintypes.HLOCAL


def _blob_from_bytes(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    # Keep a reference so the buffer isn't garbage collected while the
    # blob (which only holds a raw pointer into it) is still in use.
    blob._buffer_keepalive = buf
    return blob


def encrypt(plain_text: str) -> bytes:
    """Encrypt a string using DPAPI, scoped to the current Windows user."""
    data = plain_text.encode("utf-8")
    in_blob = _blob_from_bytes(data)
    out_blob = DATA_BLOB()

    ok = _CryptProtectData(
        ctypes.byref(in_blob),
        None,  # description
        None,  # optional entropy
        None,  # reserved
        None,  # prompt struct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")

    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _LocalFree(out_blob.pbData)

    return result


def decrypt(cipher_bytes: bytes) -> str:
    """
    Decrypt bytes produced by encrypt(). Only works for the same Windows
    user account on the same machine that originally called encrypt() --
    this is intentional.
    """
    in_blob = _blob_from_bytes(cipher_bytes)
    out_blob = DATA_BLOB()

    ok = _CryptUnprotectData(
        ctypes.byref(in_blob),
        None,  # description (out param, unused here)
        None,  # optional entropy
        None,  # reserved
        None,  # prompt struct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")

    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _LocalFree(out_blob.pbData)

    return result.decode("utf-8")
