from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.auth import get_current_user
from app.models.user import User
from app.templating import templates

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.tasks_service import get_tasks
    tasks = get_tasks(db, user_id=user.id)
    return templates.TemplateResponse(request, "tasks/index.html", {
        "tasks": tasks,
        "total": len(tasks),
    })
