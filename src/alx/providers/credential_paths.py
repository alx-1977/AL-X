"""Shared credential-path guard for design and reference reads."""


def denied(path: str) -> bool:
    parts = path.casefold().split("/")
    # Treat credential-shaped directory names as protected too.
    return any(
        part in (".alx", ".env", ".netrc", ".npmrc")
        or part in ("id_rsa", "id_rsa_sk", "id_dsa", "id_dsa_sk", "id_ecdsa", "id_ecdsa_sk",
                    "id_ed25519", "id_ed25519_sk")
        or part.startswith((".env.", "id_rsa"))
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        or any(word in part for word in ("secret", "credential", "token", "password"))
        for part in parts
    )

