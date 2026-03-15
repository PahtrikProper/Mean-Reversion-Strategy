"""API Key Input Prompt Utility

Handles prompting for API keys and persisting them for use by other scripts.
Credentials are stored securely and loaded on subsequent runs.
"""

import os
import sys
import json
import getpass
from typing import Tuple, Optional
from pathlib import Path

# Credentials file location (~/.bybit_credentials.json)
CREDS_FILE = Path.home() / ".bybit_credentials.json"

def _save_credentials(api_key: str, api_secret: str) -> None:
    """Save credentials to persistent storage file."""
    try:
        creds_data = {
            "api_key": api_key,
            "api_secret": api_secret
        }
        with open(CREDS_FILE, "w", encoding="utf-8") as f:
            json.dump(creds_data, f)
        # Restrict file access to the current user only (cross-platform)
        if sys.platform == "win32":
            import subprocess
            subprocess.run(
                ["icacls", str(CREDS_FILE), "/inheritance:r",
                 "/grant:r", f"{os.getenv('USERNAME', '')}:(R,W)"],
                capture_output=True,
            )
        else:
            os.chmod(CREDS_FILE, 0o600)
    except Exception as exc:
        print(f"⚠ Warning: Could not save credentials to {CREDS_FILE}: {exc}")

def _load_credentials() -> Optional[Tuple[str, str]]:
    """Load credentials from persistent storage file."""
    if not CREDS_FILE.exists():
        return None
    try:
        with open(CREDS_FILE, "r") as f:
            creds_data = json.load(f)
        api_key = creds_data.get("api_key", "").strip()
        api_secret = creds_data.get("api_secret", "").strip()
        if api_key and api_secret:
            return api_key, api_secret
    except Exception as exc:
        print(f"⚠ Warning: Could not load credentials from {CREDS_FILE}: {exc}")
    return None

def get_api_credentials() -> Tuple[str, str]:
    """
    Get Bybit API credentials from environment variables, saved file, or prompt user.

    Priority order:
    1. BYBIT_API_KEY and BYBIT_API_SECRET environment variables
    2. Saved credentials from ~/.bybit_credentials.json
    3. Prompt user to enter via terminal (hidden input for security)

    Once obtained, credentials are saved for future use by other scripts.

    Returns:
        Tuple of (api_key, api_secret)

    Raises:
        RuntimeError: If credentials cannot be obtained or are invalid
    """
    # Try environment variables first
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()

    if api_key and api_secret and not api_key.startswith("YOUR_") and not api_secret.startswith("YOUR_"):
        print("✓ API credentials loaded from environment variables")
        return api_key, api_secret

    # Try saved credentials file
    saved_creds = _load_credentials()
    if saved_creds:
        api_key, api_secret = saved_creds
        print("✓ API credentials loaded from saved file")
        return api_key, api_secret

    # Check if running in non-interactive environment
    if not sys.stdin.isatty():
        raise RuntimeError(
            "✗ Bybit API credentials not found.\n"
            "Please set BYBIT_API_KEY and BYBIT_API_SECRET environment variables or run in interactive mode.\n"
            "This application requires valid credentials to operate.\n"
            "Visit: https://www.bybit.com/en/user-center/account-api to create API credentials."
        )

    # Prompt user if not found elsewhere
    print("\n" + "="*65)
    print("🔐 Bybit API Credentials Setup")
    print("="*65)
    print("\nPlease enter your Bybit API credentials.")
    print("These will be saved locally for use by other scripts.")
    print("(Your input will be hidden for security)")
    print("Get API credentials at: https://www.bybit.com/en/user-center/account-api\n")

    attempts = 0
    max_attempts = 3

    while attempts < max_attempts:
        try:
            api_key = getpass.getpass("Enter your Bybit API Key: ").strip()
            if not api_key:
                print("✗ API Key cannot be empty. Please try again.")
                attempts += 1
                continue

            api_secret = getpass.getpass("Enter your Bybit API Secret: ").strip()
            if not api_secret:
                print("✗ API Secret cannot be empty. Please try again.")
                attempts += 1
                continue

            # Basic validation
            if len(api_key) < 10:
                print("✗ API Key appears too short. Please verify and try again.")
                attempts += 1
                continue
            if len(api_secret) < 10:
                print("✗ API Secret appears too short. Please verify and try again.")
                attempts += 1
                continue

            # Confirm credentials
            print(f"\nAPI Key (last 8 chars): ...{api_key[-8:]}")
            print(f"API Secret (last 8 chars): ...{api_secret[-8:]}")
            confirm = input("Are these credentials correct? (yes/no): ").strip().lower()

            if confirm in ("yes", "y"):
                # Save credentials for future use
                _save_credentials(api_key, api_secret)
                print(f"✓ API credentials confirmed and saved")
                return api_key, api_secret
            else:
                attempts += 1
                print(f"Please re-enter your credentials. (Attempt {attempts}/{max_attempts})\n")
        except KeyboardInterrupt:
            raise RuntimeError("✗ API credential input cancelled by user")

    raise RuntimeError(
        f"✗ Failed to obtain valid API credentials after {max_attempts} attempts.\n"
        "Please try again or set BYBIT_API_KEY and BYBIT_API_SECRET environment variables."
    )


def validate_api_credentials(api_key: str, api_secret: str) -> bool:
    """
    Validate that API credentials are not placeholder strings.

    Args:
        api_key: The API key to validate
        api_secret: The API secret to validate

    Returns:
        True if credentials are valid (not placeholders), False otherwise
    """
    placeholders = ("YOUR_BYBIT_API_KEY", "YOUR_BYBIT_API_SECRET", "")

    if api_key in placeholders or api_secret in placeholders:
        return False

    return bool(api_key and api_secret)


def ensure_api_credentials():
    """
    Ensure API credentials are set and valid.
    Updates the constants module with credentials if needed.

    This should be called at application startup before any API operations.
    Credentials are obtained from (in order):
    1. Environment variables (BYBIT_API_KEY, BYBIT_API_SECRET)
    2. Saved credentials file (~/.bybit_credentials.json)
    3. Interactive prompt (will save for future use)

    Raises:
        RuntimeError: If valid credentials cannot be obtained
    """
    from . import constants

    # Check if current credentials are valid
    if validate_api_credentials(constants.API_KEY, constants.API_SECRET):
        print("✓ API credentials are valid")
        return

    # Get credentials from environment, saved file, or prompt user
    try:
        api_key, api_secret = get_api_credentials()
    except RuntimeError as exc:
        print(str(exc))
        raise

    # Update constants
    constants.API_KEY = api_key
    constants.API_SECRET = api_secret

    print("✓ API credentials have been set successfully\n")
