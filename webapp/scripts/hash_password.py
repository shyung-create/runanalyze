#!/usr/bin/env python3
"""Generate an argon2id hash for DASHBOARD_PASSWORD_HASH.

Run on the instance, paste the output into the 0600 .env file. Never
prints the password back, never writes it anywhere — reads it once via
getpass and discards it as soon as the hash is computed.

Usage:
    .venv/bin/python webapp/scripts/hash_password.py
"""
from __future__ import annotations

import getpass

from argon2 import PasswordHasher


def main() -> None:
    password = getpass.getpass("Dashboard password (input hidden): ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords did not match.")
        raise SystemExit(1)
    print(PasswordHasher().hash(password))


if __name__ == "__main__":
    main()
