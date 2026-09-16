"""One-time local OAuth bootstrap. Never run on Railway or commit secret files."""
import argparse
import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def save_secrets(path, values):
    # Exclusive creation avoids accidentally overwriting existing credentials.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(values, out, indent=2)
        out.write("\n")

def main():
    parser = argparse.ArgumentParser(description="Authorize MAGI1 Drive access on your own PC")
    parser.add_argument("--client-json", required=True, type=Path,
                        help="Downloaded Desktop OAuth client JSON (keep outside repository)")
    args = parser.parse_args()
    destination = Path.home() / ".magi1" / "railway-drive-variables.json"
    if destination.exists():
        parser.error("Credential file already exists under your home .magi1 directory; preserve it before reauthorizing.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_json), SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True,
        authorization_prompt_message="",
        success_message="MAGI1 authorization completed. You may close this window.",
        access_type="offline", prompt="consent", timeout_seconds=300)
    if not credentials.refresh_token:
        raise RuntimeError("No refresh token received; no credential file was written.")
    credentials.refresh(Request())
    save_secrets(destination, {
        "MAGI1_GOOGLE_CLIENT_ID": credentials.client_id,
        "MAGI1_GOOGLE_CLIENT_SECRET": credentials.client_secret,
        "MAGI1_GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
    })
    print("Authorization and refresh succeeded.")
    print("Saved privately to: " + str(destination))
    print("Paste this JSON only into Railway MAGI1-Flow Variables / Raw Editor.")
    print("Do not send it in chat, upload it to Drive, or commit it to GitHub.")
    print("Uploader is not enabled by this helper. It creates no Drive files.")

if __name__ == "__main__":
    main()
