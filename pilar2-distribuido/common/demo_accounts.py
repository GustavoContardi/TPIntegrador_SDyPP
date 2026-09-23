"""Cuentas demo: identidades fijas con su minero ya desplegado (AGENT.md 3.1).

Son custodiales —la privada la tiene el backend, el navegador no firma— y por
eso sus mineros **no** tienen ``worker:owner:*`` en Redis: el vínculo dueño →
minero vive acá. Estaba sólo en el router de cuentas del API; se movió a
``common`` porque el NCT también lo necesita para saber quién puede proponer
(``common.blockchain.proposers``), y dos copias de la tabla terminan divergiendo.
"""

from __future__ import annotations

from typing import Optional

# Coinciden con los workers de demo-deployments.yaml.
DEMO_ACCOUNTS = {
    "valentin": {
        "worker_id": "worker-standalone",
        "mode": "standalone",
        "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEK+zAT5RDdx+IZeFJyMt5n+Sq3bofPSTONUdH0rIoafqek0B9z2+Ce+KOpF4d7HF9MMCaEdvf79DuXgTyi6w1gg==",
    },
    "gustavo": {
        "worker_id": "worker-pool-coordinator",
        "mode": "pool-coordinator",
        "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESZSf6/KLGtCWzykPJNwplTtLIXfV7Q8bWzCXpSt0UXdDUwRGoRMCipOtVppZ5+OK8h5Rth5HpbUFgdNa4hz+Qg==",
    },
    "matt": {
        "worker_id": "worker-pool-miner-1",
        "mode": "pool-worker",
        "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgjnoP4I9rGjo0m4AdnXvtiSKArLmVQwW0QPJ4/psGbysWgLDKuQZcLkRkZOqrV7405qF5mIxfDfU8xjQgHEQig==",
    },
    "profesor1": {
        "worker_id": "worker-pool-miner-2",
        "mode": "pool-worker",
        "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEfixrZ70q1AbkT9XkjN4A+7BnasOneDR157dLyF0ITlFwLKhuFc3WfcGxupm9xY4XXZay6BIRSwzUNCJZHGFAcw==",
    },
    "profesor2": {
        "worker_id": "worker-pool-miner-3",
        "mode": "pool-worker",
        "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEe7p253GK4YRVqNZ2AVTfex6Wv4lIFRNTusdMlRT416cMTQr2WrvDSE3LPG4BiUXEIzwP53R0aVTp5uOfUfXmVQ==",
    },
}


def demo_account_by_pubkey(pubkey: str) -> Optional[tuple[str, dict]]:
    """``(username, cuenta)`` de la cuenta demo con esta pubkey, o ``None``."""
    for username, account in DEMO_ACCOUNTS.items():
        if account["pubkey"] == pubkey:
            return username, account
    return None
