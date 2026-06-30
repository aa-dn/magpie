import os
import requests

_BUCKET = "thumbnails"


def upload_thumbnail(image_bytes: bytes, filename: str) -> str | None:
    """Upload image bytes to Supabase Storage and return the public URL, or None on failure."""
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key  = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not service_key:
        return None

    suffix = os.path.splitext(filename)[1].lower()
    content_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")

    resp = requests.post(
        f"{supabase_url}/storage/v1/object/{_BUCKET}/{filename}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "Content-Type": content_type,
        },
        data=image_bytes,
        timeout=30,
    )
    if resp.status_code in (200, 201):
        return f"{supabase_url}/storage/v1/object/public/{_BUCKET}/{filename}"
    raise RuntimeError(f"Supabase upload failed {resp.status_code}: {resp.text[:300]}")
