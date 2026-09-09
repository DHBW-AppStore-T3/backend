import base64
import hashlib
import hmac
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Course

router = APIRouter()


def _verify_oauth_signature(method: str, url: str, params: dict, consumer_secret: str) -> bool:
    filtered = {k: v for k, v in params.items() if k != "oauth_signature"}
    sorted_params = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(filtered.items())
    )
    base_string = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(sorted_params, safe=""),
    ])
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&"
    expected = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    provided = params.get("oauth_signature", "")
    return hmac.compare_digest(expected, provided)


@router.post("/lti/launch", response_class=HTMLResponse)
async def lti_launch(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    params = dict(form)

    consumer_key = params.get("oauth_consumer_key", "")
    if consumer_key != settings.LTI_CONSUMER_KEY:
        raise HTTPException(status_code=403, detail="Invalid consumer key")

    launch_url = f"{request.url.scheme}://{request.url.netloc}{request.url.path}"

    if not _verify_oauth_signature(
        method="POST",
        url=launch_url,
        params=params,
        consumer_secret=settings.LTI_CONSUMER_SECRET,
    ):
        raise HTTPException(status_code=403, detail="Invalid OAuth signature")

    user_name = params.get(
        "lis_person_name_full",
        params.get("lis_person_name_given", "Nutzer"),
    )
    user_email = params.get("lis_person_contact_email_primary", "")
    course_title = params.get("context_title", "")
    roles = params.get("roles", "").lower()
    role = "instructor" if any(r in roles for r in ("instructor", "teacher", "admin")) else "student"

    # Auto-create course in app-store if it doesn't exist yet
    course_id = ""
    if course_title:
        course = db.query(Course).filter(Course.name == course_title).first()
        if not course:
            course = Course(name=course_title)
            db.add(course)
            db.commit()
            db.refresh(course)
        course_id = str(course.courseId)

    query = urllib.parse.urlencode({
        "lti": "1",
        "name": user_name,
        "email": user_email,
        "course": course_title,
        "courseId": course_id,
        "role": role,
    })
    target = f"{settings.APP_BASE_URL}?{query}"

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script>window.location.href = "{target}";</script>
</head><body>Weiterleitung...</body></html>
""")
