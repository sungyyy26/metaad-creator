"""Google Drive upload via a service account — mirrors a creative file into a
Drive folder right after it's uploaded to Meta's ad library.

Setup (one-time):
  1. In Google Cloud Console, create a project (or reuse one) and enable the
     "Google Drive API".
  2. Create a Service Account, then a JSON key for it, and download the file.
  3. Open the target Drive folder in a browser and share it with the service
     account's email (looks like xxx@yyy.iam.gserviceaccount.com), Editor access.
  4. Set GOOGLE_SERVICE_ACCOUNT_FILE (path to the JSON key) and
     GOOGLE_DRIVE_FOLDER_ID (the folder's ID from its URL) in .env.
"""
import json

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


class DriveApiError(RuntimeError):
    pass


def _get_access_token(service_account_file):
    try:
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    except (OSError, ValueError) as e:
        raise DriveApiError(f"서비스 계정 키 파일을 읽지 못했습니다 ({service_account_file}): {e}")
    creds.refresh(GoogleAuthRequest())
    return creds.token


def upload_file(service_account_file, folder_id, filename, file_obj, mimetype="application/octet-stream"):
    token = _get_access_token(service_account_file)
    metadata = {"name": filename, "parents": [folder_id]}
    files = {
        "metadata": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
        "file": (filename, file_obj, mimetype),
    }
    r = requests.post(UPLOAD_URL, headers={"Authorization": f"Bearer {token}"}, files=files)
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise DriveApiError(err.get("message", str(err)))
    if "id" not in data:
        raise DriveApiError(f"예상치 못한 Drive 응답: {data}")
    return {
        "file_id": data["id"],
        "url": data.get("webViewLink") or f"https://drive.google.com/file/d/{data['id']}/view",
    }


def list_subfolders(service_account_file, parent_folder_id):
    """Immediate (non-recursive) subfolders of parent_folder_id, for a
    dropdown letting the user pick where inside the shared root folder a
    given upload should land. Returns [] (never raises) on any failure —
    the caller falls back to uploading straight into the root folder, and a
    misconfigured Drive setup shouldn't take down the whole dashboard page."""
    try:
        token = _get_access_token(service_account_file)
        query = f"'{parent_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        r = requests.get(
            FILES_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "fields": "files(id,name)", "pageSize": 200, "orderBy": "name"},
        )
        data = r.json()
        return [{"id": f["id"], "name": f["name"]} for f in data.get("files", [])]
    except Exception:  # noqa: BLE001
        return []
