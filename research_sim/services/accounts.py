from __future__ import annotations

import re
from typing import List

from ..database.models import SenderAccount
from ..database.requests import DatabaseRequests


ACCOUNT_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


class AccountAlreadyExists(ValueError):
    pass


class SenderAccountService:
    def __init__(self, requests: DatabaseRequests) -> None:
        self.requests = requests

    def add(self, account_key: str, label: str) -> SenderAccount:
        normalized_key = account_key.strip().lower()
        clean_label = label.strip()
        if not ACCOUNT_KEY_PATTERN.fullmatch(normalized_key):
            raise ValueError("account_key may contain only letters, digits, '_' and '-' (max 48)")
        if not clean_label or len(clean_label) > 80:
            raise ValueError("label must contain 1 to 80 characters")
        try:
            return self.requests.add_sender_account(normalized_key, clean_label)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise AccountAlreadyExists("account_key already exists") from exc
            raise

    def list(self) -> List[SenderAccount]:
        return self.requests.list_sender_accounts()

    def set_enabled(self, account_key: str, enabled: bool) -> bool:
        if not ACCOUNT_KEY_PATTERN.fullmatch(account_key):
            raise ValueError("invalid account_key")
        return self.requests.set_sender_account_enabled(account_key, enabled)