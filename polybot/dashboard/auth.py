"""
Authentication, session management, and action authorization for the Polymarket workstation dashboard.
Implements PBKDF2-HMAC-SHA256 password/PIN hashing, constant-time validation,
cryptographic session tokens, and IP-level brute-force lockout protection.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("polybot.dashboard.auth")

PBKDF2_ROUNDS = 100_000


def hash_secret(secret: str, salt: Optional[str] = None) -> str:
    """Hashes a password or PIN using PBKDF2-HMAC-SHA256 with a cryptographically random salt."""
    if not secret:
        return ""
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ROUNDS,
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${key.hex()}"


def verify_secret(secret: str, stored_hash: str) -> bool:
    """
    Verifies a plain secret against a stored hash in constant time.
    Supports stored PBKDF2 hashes or direct plain text comparison fallback.
    """
    if not secret or not stored_hash:
        return False

    # Check if stored_hash is in standard pbkdf2 format: algorithm$iterations$salt$hash
    parts = stored_hash.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
            salt = parts[2]
            expected_key = parts[3]
            computed_key = hashlib.pbkdf2_hmac(
                "sha256",
                secret.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            ).hex()
            return hmac.compare_digest(computed_key, expected_key)
        except Exception as e:
            log.error("Error verifying PBKDF2 hash: %s", e)
            return False

    # Fallback to constant-time plain text string match if unhashed string provided in .env
    return hmac.compare_digest(secret, stored_hash)


class AuthManager:
    """
    Stateful authentication and session manager.
    Tracks active sessions, verifies logins, and enforces brute-force lockout.
    """

    def __init__(
        self,
        username: str = "admin",
        password_hash: str = "",
        pin_hash: str = "",
        session_secret: Optional[str] = None,
        session_expiry_sec: int = 86400,  # 24 hours
        enabled: bool = True,
        max_failed_attempts: int = 5,
        lockout_duration_sec: int = 900,  # 15 minutes
    ):
        self.enabled = enabled
        self.username = username
        self.password_hash = password_hash
        self.pin_hash = pin_hash
        self.session_secret = session_secret or secrets.token_hex(32)
        self.session_expiry_sec = session_expiry_sec
        self.max_failed_attempts = max_failed_attempts
        self.lockout_duration_sec = lockout_duration_sec

        # In-memory session store: token -> (username, expiry_timestamp)
        self._sessions: Dict[str, Tuple[str, float]] = {}
        self._revoked_tokens: set[str] = set()

        # In-memory IP failure tracking: ip -> list of failure timestamps
        self._ip_failures: Dict[str, List[float]] = {}

    def is_ip_locked(self, ip: str) -> bool:
        """Checks if an IP address is currently locked out due to excessive failed attempts."""
        if not ip:
            return False
        now = time.time()
        failures = [t for t in self._ip_failures.get(ip, []) if now - t < self.lockout_duration_sec]
        self._ip_failures[ip] = failures
        return len(failures) >= self.max_failed_attempts

    def record_login_failure(self, ip: str) -> None:
        """Records a failed login attempt for an IP address."""
        if not ip:
            return
        now = time.time()
        failures = [t for t in self._ip_failures.get(ip, []) if now - t < self.lockout_duration_sec]
        failures.append(now)
        self._ip_failures[ip] = failures
        if len(failures) >= self.max_failed_attempts:
            log.warning("IP %s locked out for %d seconds due to %d failed attempts",
                        ip, self.lockout_duration_sec, len(failures))

    def clear_login_failures(self, ip: str) -> None:
        """Clears failure history upon successful authentication."""
        if ip in self._ip_failures:
            del self._ip_failures[ip]

    def verify_login(self, username: str, password: str, ip: str = "") -> Tuple[bool, str]:
        """
        Validates login credentials with brute-force protection.
        Returns (success: bool, error_message: str).
        """
        if not self.enabled:
            return True, ""

        if self.is_ip_locked(ip):
            return False, f"Too many failed login attempts. IP locked for {self.lockout_duration_sec // 60} minutes."

        # Constant-time comparison for username
        username_match = hmac.compare_digest(username.strip(), self.username.strip())
        password_match = verify_secret(password, self.password_hash)

        if username_match and password_match:
            self.clear_login_failures(ip)
            log.info("Successful dashboard login for user '%s' from IP %s", username, ip or "unknown")
            return True, ""

        self.record_login_failure(ip)
        log.warning("Failed dashboard login attempt for user '%s' from IP %s", username, ip or "unknown")
        return False, "Invalid username or password"

    def verify_pin(self, pin: str) -> bool:
        """Validates the 4-digit critical action security PIN."""
        if not self.enabled:
            return True
        if not self.pin_hash:
            return True  # If no PIN configured, action is authorized
        return verify_secret(pin.strip(), self.pin_hash)

    def create_session(self, username: str) -> str:
        """Generates a cryptographically signed HMAC session token."""
        expiry = int(time.time() + self.session_expiry_sec)
        nonce = secrets.token_hex(8)
        payload = f"{username}:{expiry}:{nonce}"
        sig = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        token = f"{payload}:{sig}"
        self._sessions[token] = (username, float(expiry))
        self._cleanup_expired_sessions()
        return token

    def validate_session(self, token: Optional[str]) -> Optional[str]:
        """
        Validates session token using cryptographic HMAC signature and expiry.
        Compatible across multi-process bot instances sharing DASHBOARD_SESSION_SECRET.
        """
        if not self.enabled:
            return "anonymous"

        if not token:
            return None

        # Check local revocation
        if token in self._revoked_tokens:
            return None

        # 1. Fast local memory check
        if token in self._sessions:
            username, expiry = self._sessions[token]
            if time.time() > expiry:
                del self._sessions[token]
                return None
            return username

        # 2. Cryptographic signature check (for cross-process verification between live & paper)
        try:
            parts = token.split(":")
            if len(parts) == 4:
                username, expiry_str, nonce, expected_sig = parts
                expiry = int(expiry_str)
                if time.time() > expiry:
                    return None
                payload = f"{username}:{expiry}:{nonce}"
                computed_sig = hmac.new(
                    self.session_secret.encode("utf-8"),
                    payload.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                if hmac.compare_digest(computed_sig, expected_sig):
                    self._sessions[token] = (username, float(expiry))
                    return username
        except Exception:
            return None

        return None

    def invalidate_session(self, token: str) -> None:
        """Invalidates a session token (logout)."""
        if token in self._sessions:
            del self._sessions[token]
        self._revoked_tokens.add(token)

    def _cleanup_expired_sessions(self) -> None:
        """Removes expired sessions from memory."""
        now = time.time()
        expired = [tok for tok, (_, exp) in self._sessions.items() if now > exp]
        for tok in expired:
            del self._sessions[tok]
