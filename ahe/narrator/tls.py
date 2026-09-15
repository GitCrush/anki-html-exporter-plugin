"""A certificate of our own, so the page can be opened over HTTPS.

Browsers open the microphone only on a secure page; on this machine
``127.0.0.1`` counts, but a phone on the network reaching the page by its
address does not. So the network listener speaks TLS, with a certificate
made here and kept in ``user_files/tls``. Self-signed: the phone shows a
warning once, and the page is a secure context from then on.

Where the ``openssl`` command exists it makes the certificate; where it does
not -- Anki on Windows, typically -- the same certificate is put together
here: an RSA key, and the X.509 structure written out in DER by hand. Anki's
Python has no cryptography library, and an add-on is in no position to
require one.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import random
import shutil
import subprocess
from pathlib import Path

DAYS = 3650
KEY_BITS = 2048
COMMON_NAME = "Anki Narrator"


def ensure_certificate(directory: Path, addresses: list[str]) -> tuple[Path, Path]:
    """The certificate and key in ``directory``, made if not there yet.

    The addresses the machine has now go in as names the certificate is
    valid for; one it did not have when made is still served, with the same
    warning as any self-signed one.
    """
    cert, key = directory / "cert.pem", directory / "key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    directory.mkdir(parents=True, exist_ok=True)
    names = ["localhost"]
    ips = ["127.0.0.1"] + [a for a in addresses if a not in ("127.0.0.1", "") and _is_ip(a)]
    if shutil.which("openssl"):
        try:
            _with_openssl(cert, key, names, ips)
            return cert, key
        except (OSError, subprocess.CalledProcessError):
            pass
    pem_cert, pem_key = make_certificate(names, ips)
    key.write_bytes(pem_key)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    cert.write_bytes(pem_cert)
    return cert, key


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _with_openssl(cert: Path, key: Path, names: list[str], ips: list[str]) -> None:
    san = ",".join([f"DNS:{n}" for n in names] + [f"IP:{i}" for i in ips])
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", f"rsa:{KEY_BITS}", "-nodes", "-sha256",
         "-keyout", str(key), "-out", str(cert), "-days", str(DAYS),
         "-subj", f"/CN={COMMON_NAME}", "-addext", f"subjectAltName={san}",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign"],
        check=True, capture_output=True, timeout=120,
    )
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass


# The certificate, by hand
######################################################################


def make_certificate(names: list[str], ips: list[str], bits: int = KEY_BITS) -> tuple[bytes, bytes]:
    """A self-signed certificate and its RSA key, both as PEM."""
    n, e, d, p, q = _rsa_key(bits)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    not_before = now
    not_after = now + datetime.timedelta(days=DAYS)
    serial = random.getrandbits(63) | 1

    name = _seq(_set(_seq(_oid("2.5.4.3"), _utf8(COMMON_NAME))))
    algorithm = _seq(_oid("1.2.840.113549.1.1.11"), b"\x05\x00")  # sha256WithRSAEncryption
    public_key = _seq(
        _seq(_oid("1.2.840.113549.1.1.1"), b"\x05\x00"),  # rsaEncryption
        _bits(_seq(_int(n), _int(e))),
    )
    san = b"".join([_tag(2, n_.encode("ascii")) for n_ in names]
                   + [_tag(7, ipaddress.ip_address(i).packed) for i in ips])
    extensions = _seq(
        _seq(_oid("2.5.29.17"), _octets(_seq(san))),                     # subjectAltName
        _seq(_oid("2.5.29.19"), _bool(True), _octets(_seq(_bool(True)))),  # basicConstraints CA:TRUE
        # keyUsage: digitalSignature, keyEncipherment, keyCertSign -- bits 0, 2
        # and 5 of one octet, two trailing bits unused
        _seq(_oid("2.5.29.15"), _bool(True), _octets(_tlv(0x03, b"\x02\xa4"))),
    )
    tbs = _seq(
        _explicit(0, _int(2)),  # v3
        _int(serial),
        algorithm,
        name,
        _seq(_utc(not_before), _utc(not_after)),
        name,
        public_key,
        _explicit(3, extensions),
    )
    signature = _sign(tbs, d, n)
    certificate = _seq(tbs, algorithm, _bits(signature))

    dp, dq, qinv = d % (p - 1), d % (q - 1), pow(q, -1, p)
    private = _seq(_int(0), _int(n), _int(e), _int(d), _int(p), _int(q), _int(dp), _int(dq), _int(qinv))
    return _pem("CERTIFICATE", certificate), _pem("RSA PRIVATE KEY", private)


def _sign(data: bytes, d: int, n: int) -> bytes:
    """PKCS#1 v1.5 with SHA-256."""
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
    length = (n.bit_length() + 7) // 8
    padded = b"\x00\x01" + b"\xff" * (length - len(digest_info) - 3) + b"\x00" + digest_info
    return pow(int.from_bytes(padded, "big"), d, n).to_bytes(length, "big")


# RSA
######################################################################


def _rsa_key(bits: int) -> tuple[int, int, int, int, int]:
    e = 65537
    while True:
        p = _prime(bits // 2)
        q = _prime(bits // 2)
        if p == q:
            continue
        n = p * q
        phi = (p - 1) * (q - 1)
        if n.bit_length() != bits or phi % e == 0:
            continue
        return n, e, pow(e, -1, phi), p, q


_SMALL_PRIMES = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97]


def _prime(bits: int) -> int:
    rng = random.SystemRandom()
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if any(candidate % s == 0 for s in _SMALL_PRIMES):
            continue
        if _probably_prime(candidate, rng):
            return candidate


def _probably_prime(n: int, rng: random.SystemRandom, rounds: int = 40) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


# DER
######################################################################


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _length(len(content)) + content


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 8) // 8, "big") or b"\x00"
    return _tlv(0x02, raw)


def _bool(value: bool) -> bytes:
    return _tlv(0x01, b"\xff" if value else b"\x00")


def _octets(content: bytes) -> bytes:
    return _tlv(0x04, content)


def _bits(content: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + content)


def _utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _utc(moment: datetime.datetime) -> bytes:
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _explicit(number: int, content: bytes) -> bytes:
    return _tlv(0xA0 | number, content)


def _tag(number: int, content: bytes) -> bytes:
    return _tlv(0x80 | number, content)


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for value in parts[2:]:
        chunk = [value & 0x7F]
        value >>= 7
        while value:
            chunk.append(0x80 | (value & 0x7F))
            value >>= 7
        body.extend(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _pem(label: str, der: bytes) -> bytes:
    import base64

    body = base64.encodebytes(der).replace(b"\n", b"")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return b"".join([f"-----BEGIN {label}-----\n".encode()] + [line + b"\n" for line in lines]
                    + [f"-----END {label}-----\n".encode()])
