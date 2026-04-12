from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.fx_service import COMMON_CURRENCIES
from app.services.property_valuation import provider_status
from app.services.user_profile_service import get_profile, update_profile
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])

# Common countries for the residence / nationality dropdowns
COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Argentina", "Australia", "Austria",
    "Bahrain", "Bangladesh", "Belgium", "Brazil", "Canada", "Chile", "China",
    "Colombia", "Croatia", "Czech Republic", "Denmark", "Egypt", "Finland",
    "France", "Germany", "Ghana", "Greece", "Hong Kong", "Hungary", "India",
    "Indonesia", "Ireland", "Israel", "Italy", "Japan", "Jordan", "Kenya",
    "Kuwait", "Luxembourg", "Malaysia", "Mexico", "Morocco", "Netherlands",
    "New Zealand", "Nigeria", "Norway", "Pakistan", "Peru", "Philippines",
    "Poland", "Portugal", "Qatar", "Romania", "Russia", "Saudi Arabia",
    "Singapore", "South Africa", "South Korea", "Spain", "Sri Lanka",
    "Sweden", "Switzerland", "Taiwan", "Thailand", "Turkey", "Ukraine",
    "United Arab Emirates", "United Kingdom", "United States", "Vietnam",
]


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    profile = get_profile(db)
    prop_status = {
        region: provider_status(
            currency={"US": "USD", "UK": "GBP", "AU": "AUD"}.get(region, "USD"),
            country_of_residence=profile.country_of_residence,
            rentcast_api_key=profile.rentcast_api_key,
            property_data_api_key=profile.property_data_api_key,
            domain_api_key=profile.domain_api_key,
        )
        for region in ("US", "UK", "AU")
    }
    return templates.TemplateResponse(request, "settings/index.html", {
        "profile": profile,
        "currencies": COMMON_CURRENCIES,
        "countries": COUNTRIES,
        "saved": request.query_params.get("saved"),
        "prop_status": prop_status,
    })


@router.post("")
def settings_save(
    request: Request,
    display_currency: str = Form("USD"),
    country_of_residence: str = Form(""),
    nationality: str = Form(""),
    has_spouse: bool = Form(False),
    spouse_nationality: str = Form(""),
    rentcast_api_key: str = Form(""),
    property_data_api_key: str = Form(""),
    domain_api_key: str = Form(""),
    db: Session = Depends(get_db),
):
    update_profile(
        db,
        display_currency=display_currency,
        country_of_residence=country_of_residence.strip() or None,
        nationality=nationality.strip() or None,
        has_spouse=has_spouse,
        spouse_nationality=spouse_nationality.strip() or None,
        rentcast_api_key=rentcast_api_key or None,
        property_data_api_key=property_data_api_key or None,
        domain_api_key=domain_api_key or None,
    )

    # Ensure current FX rates exist for the chosen display currency
    if display_currency.upper() != "USD":
        try:
            from app.services.fx_rate_fetcher import sync_current_rates
            sync_current_rates(db, base="USD", quotes=[display_currency.upper()])
        except Exception:
            pass

    return RedirectResponse(url="/settings?saved=1", status_code=303)
