# MAGI1 Drive OAuth setup (one-time PC authorization)

Status: setup helper only. Archive upload, retention and permanent deletion are NOT
implemented or activated by this guide. Use your personal PC and your own Google account.

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
   Apply/deploy when ready; adding variables alone does not create an uploader.
9. Planned uploader setup will create its own MAGI1-Archive folder through the same
   OAuth client and record its ID. Do not assume drive.file can see a manually created
   folder or a folder created by the ChatGPT connector without an explicit grant.
10. Before activation: upload a test file, download/check its hash, verify restart
    recovery, and verify account/quota. Only then enable archive and retention.

The variable names are the agreed interface for the future uploader; the current
collector does not yet read them. The ChatGPT Drive connection is independent and
does not transfer a refresh token to Railway.

Policy: raw events stay in Drive for a rolling 7 days and are permanently deleted only
after analysis completion and backup of derived evidence. Analysis stays on Railway
for a calendar month, then migrates to Drive indefinitely after upload verification.

References:
- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
