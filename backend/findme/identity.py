from __future__ import annotations

import base64
import hashlib


def asset_id_for(source_id: str, relative_path: str) -> str:
    identity = f"{source_id}\0{relative_path}".encode("utf-8")
    digest = hashlib.blake2b(identity, digest_size=16).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
