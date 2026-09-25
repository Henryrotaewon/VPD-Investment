# MAGI1 Drive OAuth setup (one-time PC authorization)

The collector implements verified archival in `magi1/archive.py`. This guide and
the local helper configure credentials; the helper itself never uploads or deletes
Drive files. Use your personal PC and your own Google account.

1. In https://console.cloud.google.com/ create/select project MAGI1-Archive.
2. Enable Google Drive API through APIs & Services / Library.
3. Configure Google Auth Platform branding and audience (External for a personal account).
   Add your own email as a test user while testing. Request only
   https://www.googleapis.com/auth/drive.file under Data Access.
4. Create an OAuth client of type Desktop app. Download its JSON outside the repository.
5. For unattended use, resolve the OAuth app's publishing status before issuing the
   final token: External + Testing refresh tokens for Drive normally expire after 7 days.
   Change to In production where available and satisfy the console's requirements;
   this does not mean the program or Drive files are publicly shared. Reauthorize if
   the original token was issued in Testing. Tokens can still be revoked or expire.
6. On Windows PowerShell in the repository directory, run:

~~~powershell
py -m pip install google-auth-oauthlib requests
py tools/magi1_drive_oauth.py --client-json "${env:USERPROFILE}/Downloads/client_secret.json"
~~~

Replace the filename with the downloaded JSON's actual filename. The helper starts a
local loopback callback and opens the browser. Sign into the intended 100 GB account,
verify the app and Drive permission, and consent. No Google password goes to Railway.
Official installed-app flow: https://developers.google.com/identity/protocols/oauth2/native-app

7. The helper validates token refresh and saves
   %USERPROFILE%/.magi1/railway-drive-variables.json without printing credentials.
   POSIX mode is restricted where supported; Windows users must keep this in their own
   protected profile. Do not share, commit, screenshot, or upload either secret JSON.
8. Railway -> MAGI1-Flow -> Variables -> Raw Editor: merge the three JSON keys with
   existing variables. Do NOT replace/remove MAGI1_DATA_DIR, MODE, timezone, etc.
   Apply/deploy after updating. An already enabled archiver retries with the new
   credentials after restart.
9. The uploader creates its own MAGI1-Archive folder through the same
   OAuth client. Do not assume drive.file can see a manually created
   folder or a folder created by the ChatGPT connector without an explicit grant.
10. Before activation: upload a test file, download/check its hash, verify restart
    recovery, and verify account/quota. Only then enable archive and retention.

The collector reads these variables when `MAGI1_DRIVE_ARCHIVE_ENABLED` enables
archival. The ChatGPT Drive connection is independent and does not transfer a
refresh token to Railway.

## Recovering an expired or revoked token

An HTTP 400 alone does not prove expiration. The archiver logs an allowlisted
OAuth error code without logging response bodies or credentials. `invalid_grant`
requires reauthorization; it does not distinguish Testing expiry from revocation.
`invalid_client` calls for checking the client ID/secret pair first.

1. In the original Cloud project, check Google Auth Platform / Audience. If still
   Testing, complete the console requirements for In production before issuing
   the replacement token. Changing status alone cannot restore an invalid token.
2. Use the SAME Desktop OAuth client and Google account so `drive.file` can keep
   accessing the existing app-created archives. Keep the old credentials file.
3. Update this repository on your PC, then run the helper with a new output path:

~~~powershell
py tools/magi1_drive_oauth.py --client-json "${env:USERPROFILE}/Downloads/client_secret.json" --output "${env:USERPROFILE}/.magi1/railway-drive-variables-renewed.json"
~~~

Use the actual downloaded client JSON filename. The helper verifies refresh before
saving and refuses to overwrite an existing output file. Choose another filename
for later renewals. Reauthorization needs the account holder's Google consent.

4. Merge the generated three variables into Railway MAGI1-Flow only, keeping all
   other variables. Never paste token values into chat or commit either JSON.
5. After deployment, confirm `archive_research_verified` and
   `archive_raw_verified` logs. A successful refresh alone is not proof of a backup.
   On archive failure local data is retained; do not manually delete it to free space.

Policy: raw events stay in Drive for a rolling 7 days and are permanently deleted only
after analysis completion and backup of derived evidence. Analysis stays on Railway
for a calendar month, then migrates to Drive indefinitely after upload verification.

References:
- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
