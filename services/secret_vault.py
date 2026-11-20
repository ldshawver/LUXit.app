"""
SecretVault - Encryption service for secure storage of API keys and secrets
"""
import os
import base64
from cryptography.fernet import Fernet, MultiFernet
import logging

logger = logging.getLogger(__name__)


def _is_production() -> bool:
    return os.environ.get('FLASK_ENV', '').lower() == 'production'


def load_keys(primary=None, previous=None):
    """Fernet keys from ENCRYPTION_MASTER_KEY (encrypts) followed by
    ENCRYPTION_MASTER_KEY_PREVIOUS (comma-separated, decrypt only).

    Raises ValueError on a malformed key so a typo never silently swaps in a
    throwaway key."""
    primary = os.environ.get('ENCRYPTION_MASTER_KEY', '') if primary is None else primary
    previous = os.environ.get('ENCRYPTION_MASTER_KEY_PREVIOUS', '') if previous is None else previous
    raw = [primary.strip()] + [k.strip() for k in previous.split(',') if k.strip()]
    keys = []
    for i, key in enumerate(raw):
        if not key:
            continue
        try:
            keys.append(Fernet(key.encode()))
        except Exception:
            name = 'ENCRYPTION_MASTER_KEY' if i == 0 else f'ENCRYPTION_MASTER_KEY_PREVIOUS[{i - 1}]'
            raise ValueError(f'{name} is not a valid Fernet key') from None
    if not primary.strip() and keys:
        raise ValueError('ENCRYPTION_MASTER_KEY_PREVIOUS set without ENCRYPTION_MASTER_KEY')
    return keys


class SecretVault:
    """Handles encryption and decryption of sensitive data"""
    
    def __init__(self):
        """Initialize the vault with a master key from environment"""
        self._cipher = None
        self._initialize_cipher()
    
    def _initialize_cipher(self):
        """Load the master key (plus any previous keys still being rotated out).

        Production refuses to start without a valid key: a generated key would
        encrypt new secrets under a key that dies with the process."""
        try:
            keys = load_keys()
        except ValueError as e:
            if _is_production():
                raise RuntimeError(f'SecretVault: {e}') from None
            logger.error(f"Failed to load master key: {e}")
            keys = []

        if not keys:
            if _is_production():
                raise RuntimeError('SecretVault: ENCRYPTION_MASTER_KEY is required in production')
            # Development only: secrets encrypted now are unreadable after a restart.
            logger.warning("ENCRYPTION_MASTER_KEY not found, using an ephemeral development key")
            keys = [Fernet(Fernet.generate_key())]

        self._cipher = MultiFernet(keys)

    def rotate(self, encrypted_text: str) -> str:
        """Re-encrypt a stored value under the current master key."""
        if not encrypted_text:
            return encrypted_text
        token = self._cipher.rotate(base64.b64decode(encrypted_text.encode()))
        return base64.b64encode(token).decode()
    
    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a plaintext string
        
        Args:
            plaintext: The string to encrypt
            
        Returns:
            Base64-encoded encrypted string
        """
        if not plaintext:
            return ""
        
        try:
            encrypted_bytes = self._cipher.encrypt(plaintext.encode())
            return base64.b64encode(encrypted_bytes).decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise
    
    def decrypt(self, encrypted_text: str) -> str:
        """
        Decrypt an encrypted string
        
        Args:
            encrypted_text: Base64-encoded encrypted string
            
        Returns:
            Decrypted plaintext string
        """
        if not encrypted_text:
            return ""
        
        try:
            encrypted_bytes = base64.b64decode(encrypted_text.encode())
            decrypted_bytes = self._cipher.decrypt(encrypted_bytes)
            return decrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise
    
    def mask_secret(self, secret: str, show_chars: int = 4) -> str:
        """
        Mask a secret for display purposes
        
        Args:
            secret: The secret to mask
            show_chars: Number of characters to show at the end
            
        Returns:
            Masked string (e.g., "sk-***abc123")
        """
        if not secret or len(secret) <= show_chars:
            return "****"
        
        return f"{secret[:2]}***{secret[-show_chars:]}"
    
    def encrypt_dict(self, data: dict) -> dict:
        """
        Encrypt all values in a dictionary
        
        Args:
            data: Dictionary with string values to encrypt
            
        Returns:
            Dictionary with encrypted values
        """
        if not data:
            return {}
        
        encrypted = {}
        for key, value in data.items():
            if isinstance(value, str) and value:
                encrypted[key] = self.encrypt(value)
            else:
                encrypted[key] = value
        
        return encrypted
    
    def decrypt_dict(self, data: dict) -> dict:
        """
        Decrypt all values in a dictionary
        
        Args:
            data: Dictionary with encrypted string values
            
        Returns:
            Dictionary with decrypted values
        """
        if not data:
            return {}
        
        decrypted = {}
        for key, value in data.items():
            if isinstance(value, str) and value:
                try:
                    decrypted[key] = self.decrypt(value)
                except:
                    # If decryption fails, value might not be encrypted
                    decrypted[key] = value
            else:
                decrypted[key] = value
        
        return decrypted


# Global instance
vault = SecretVault()
