from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models.category import Category, CategoryType
from app.models.category_rule import CategoryRule
from app.models.transaction import Transaction

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_class=HTMLResponse)
def categories_list(request: Request, db: Session = Depends(get_db)):
    categories = db.execute(
        select(Category).order_by(Category.category_type, Category.name)
    ).scalars().all()

    cat_stats = {}
    for cat in categories:
        count = db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.category_id == cat.id
            )
        ).scalar() or 0
        cat_stats[cat.id] = count

    rules = db.execute(
        select(CategoryRule).order_by(CategoryRule.hit_count.desc()).limit(50)
    ).scalars().all()

    return templates.TemplateResponse(request, "categories/list.html", {
        "categories": categories,
        "cat_stats": cat_stats,
        "category_types": list(CategoryType),
        "rules": rules,
    })


@router.post("/add")
def category_add(
    request: Request,
    name: str = Form(...),
    category_type: str = Form(...),
    parent_id: str = Form(""),
    db: Session = Depends(get_db),
):
    existing = db.execute(
        select(Category).where(func.lower(Category.name) == name.strip().lower())
    ).scalar_one_or_none()

    if existing:
        categories = db.execute(
            select(Category).order_by(Category.category_type, Category.name)
        ).scalars().all()
        cat_stats = {}
        for c in categories:
            cnt = db.execute(
                select(func.count(Transaction.id)).where(
                    Transaction.category_id == c.id
                )
            ).scalar() or 0
            cat_stats[c.id] = cnt
        rules = db.execute(
            select(CategoryRule).order_by(CategoryRule.hit_count.desc()).limit(50)
        ).scalars().all()
        return templates.TemplateResponse(request, "categories/list.html", {
            "categories": categories,
            "cat_stats": cat_stats,
            "category_types": list(CategoryType),
            "rules": rules,
            "error": f'Category "{name.strip()}" already exists.',
        })

    cat = Category(
        name=name.strip(),
        category_type=CategoryType(category_type),
        parent_id=int(parent_id) if parent_id.strip() else None,
        is_system=False,
    )
    db.add(cat)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/{cat_id}/edit")
def category_edit(
    cat_id: int,
    name: str = Form(...),
    category_type: str = Form(...),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if not cat:
        return HTMLResponse("Category not found", status_code=404)
    cat.name = name.strip()
    cat.category_type = CategoryType(category_type)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/{cat_id}/essential")
def category_set_essential(
    cat_id: int,
    is_essential: str = Form(...),  # "auto" | "essential" | "discretionary"
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if not cat:
        return HTMLResponse("Category not found", status_code=404)
    cat.is_essential = {
        "essential": True,
        "discretionary": False,
    }.get(is_essential.strip().lower())  # "auto" / anything else → None
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/{cat_id}/delete")
def category_delete(
    cat_id: int,
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cat_id)
    if not cat:
        return HTMLResponse("Category not found", status_code=404)

    # Unlink transactions — don't delete them
    db.execute(
        Transaction.__table__.update()
        .where(Transaction.category_id == cat_id)
        .values(category_id=None)
    )
    # Delete associated rules
    db.execute(
        CategoryRule.__table__.delete().where(CategoryRule.category_id == cat_id)
    )
    db.delete(cat)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/rules/{rule_id}/delete")
def rule_delete(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(CategoryRule, rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    return RedirectResponse(url="/categories", status_code=303)
