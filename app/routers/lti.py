import base64
import hashlib
import hmac
import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Course, User, UserRole
from app.services.lti_membership import fetch_members

logger = logging.getLogger(__name__)

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


def _map_role(ims_roles: list[str]) -> UserRole:
    """Map IMS membership roles to an app-store role.

    Moodle sends short role names ("Instructor", "Learner", ...) or full
    URNs. Anyone with an instructor/teacher/admin marker becomes a
    TEACHER; everyone else is a STUDENT.
    """
    joined = ",".join(ims_roles).lower()
    if any(marker in joined for marker in ("instructor", "teacher", "administrator")):
        return UserRole.TEACHER
    return UserRole.STUDENT


def _sync_roster(db: Session, course: Course, memberships_url: str) -> int:
    """Fetch the Moodle roster and upsert every member into ``course``.

    Returns the number of members synced. Best-effort: a failure here
    must not break the launch (the launching user still gets in), so the
    caller wraps this and swallows exceptions.
    """
    members = fetch_members(
        memberships_url,
        consumer_key=settings.LTI_CONSUMER_KEY,
        consumer_secret=settings.LTI_CONSUMER_SECRET,
    )
    count = 0
    for m in members:
        email = (m.get("email") or "").strip().lower()
        username = m.get("ext_user_username") or m.get("name") or email
        if not email:
            # Privacy setting hid the address — synthesise a stable one
            # from the username so the unique-email column is satisfied.
            if not username:
                continue
            email = f"{username}@moodle.local"

        role = _map_role(m.get("roles", []))

        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(
                email=email,
                username=username,
                firstName=m.get("given_name"),
                lastName=m.get("family_name"),
                role=role,
                courseId=course.courseId,
            )
            db.add(user)
        else:
            # Existing user (possibly Keycloak-backed): only (re)attach
            # to this course, don't clobber their role.
            user.courseId = course.courseId
        count += 1

    db.commit()
    return count


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
    synced = 0
    if course_title:
        course = db.query(Course).filter(Course.name == course_title).first()
        if not course:
            course = Course(name=course_title)
            db.add(course)
            db.commit()
            db.refresh(course)
        course_id = str(course.courseId)

        # If Moodle enabled the memberships service, the launch carries a
        # roster endpoint — pull the full member list in one shot. Best
        # effort: never let a roster hiccup break the launch itself.
        memberships_url = params.get("custom_context_memberships_url", "")
        if memberships_url:
            try:
                synced = _sync_roster(db, course, memberships_url)
            except Exception:
                logger.exception("LTI roster sync failed for course %s", course_title)

    query = urllib.parse.urlencode({
        "lti": "1",
        "name": user_name,
        "email": user_email,
        "course": course_title,
        "courseId": course_id,
        "role": role,
        "synced": synced,
    })
    target = f"{settings.APP_BASE_URL}?{query}"

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script>window.location.href = "{target}";</script>
</head><body>Weiterleitung...</body></html>
""")
