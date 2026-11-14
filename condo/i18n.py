"""English and French.

The language comes from, in order: the signed-in person's saved choice, the `lang` cookie
(set by the switch, also used on the sign-in page), then the browser's preferred language.
Money and dates follow the language: "$34,691.00" / "34 691,00 $", "Nov 6, 2026" / "6 nov. 2026".
"""
from datetime import date, datetime

from flask import request
from flask_babel import Babel, force_locale, get_locale
from babel.dates import format_date as _babel_date
from babel.dates import format_datetime as _babel_datetime
from flask_babel import gettext, ngettext  # noqa: F401  (re-exported for views)
from flask_babel.speaklater import LazyString
from markupsafe import escape


class _Lazy(LazyString):
    """A translation done when shown. Escaped in templates like any other text."""
    def __html__(self):
        return str(escape(str(self)))


def lazy_gettext(string, **variables):
    return _Lazy(gettext, string, **variables)

LANGUAGES = {"en": "English", "fr": "Français"}
DEFAULT = "en"
babel = Babel()


def choose_locale():
    try:
        from flask_login import current_user
        lang = getattr(current_user, "language", None) if current_user and current_user.is_authenticated else None
    except Exception:
        lang = None
    if lang in LANGUAGES:
        return lang
    if request and request.cookies.get("lang") in LANGUAGES:
        return request.cookies["lang"]
    if request:
        return request.accept_languages.best_match(list(LANGUAGES)) or DEFAULT
    return DEFAULT


def init_app(app):
    app.config.setdefault("BABEL_DEFAULT_LOCALE", DEFAULT)
    babel.init_app(app, locale_selector=choose_locale)

    @app.before_request
    def _fresh_language():  # decide the language for each request, never reuse an earlier one
        from flask_babel import refresh
        refresh()
    from .extensions import login_manager
    login_manager.login_message = lazy_gettext("Please sign in to see this page.")
    app.jinja_env.add_extension("jinja2.ext.i18n")
    app.jinja_env.policies["ext.i18n.trimmed"] = True
    app.jinja_env.filters.update(fdate=fdate, fdatetime=fdatetime, pct=pct)
    app.jinja_env.globals.update(lang=current_language, LANGUAGES=LANGUAGES, _month=month_name)


def current_language():
    try:
        return str(get_locale() or DEFAULT)[:2]
    except Exception:
        return DEFAULT


# Date formats by name, as CLDR patterns per language.
_PATTERNS = {
    "medium": {"en": "MMM d, y", "fr": "d MMM y"},          # Nov 6, 2026 · 6 nov. 2026
    "long": {"en": "MMMM d, y", "fr": "d MMMM y"},          # November 6, 2026 · 6 novembre 2026
    "short": {"en": "MMM d", "fr": "d MMM"},                # Nov 6 · 6 nov.
    "compact": {"en": "MMM d ''yy", "fr": "d MMM yy"},      # Nov 6 '26 · 6 nov. 26
    "month": {"en": "MMM y", "fr": "MMM y"},                # Nov 2026 · nov. 2026
    "monthname": {"en": "MMMM", "fr": "MMMM"},              # November · novembre
    "monthyear": {"en": "MMMM y", "fr": "MMMM y"},          # November 2026 · novembre 2026
    "weekday": {"en": "EEE MMM d", "fr": "EEE d MMM"},      # Fri Nov 6 · ven. 6 nov.
    "iso": {"en": "y-MM-dd", "fr": "y-MM-dd"},
}
_TIME = {"en": "h:mm a", "fr": "HH 'h' mm"}


def fdate(value, style="medium"):
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    pattern = _PATTERNS.get(style, {}).get(current_language()) or style
    return _babel_date(value, pattern, locale=current_language())


def fdatetime(value, style="medium"):
    if not value:
        return ""
    lang = current_language()
    pattern = _PATTERNS.get(style, {}).get(lang) or style
    return _babel_datetime(value, f"{pattern}, {_TIME[lang]}" if lang == "en" else f"{pattern} {_TIME[lang]}", locale=lang)


def money(cents):
    """Integer cents as money in the current language: -1250 -> '-$12.50' / '-12,50 $'."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    if current_language() == "fr":
        whole, frac = divmod(abs(cents), 100)
        return f"{sign}{whole:,}".replace(",", " ") + f",{frac:02d} $"
    return f"{sign}${abs(cents) / 100:,.2f}"


def in_language(lang):
    """Context manager: render text in someone else's language (e.g. for their notification)."""
    return force_locale(lang if lang in LANGUAGES else DEFAULT)


def lazy_money(cents):
    """Money written in whoever reads it's language (for notifications)."""
    return _Lazy(money, cents)


def lazy_date(value, style="medium"):
    return _Lazy(fdate, value, style)


def lazy_lower(text):
    """Lower-cased when shown, in the reader's language (e.g. a status inside a sentence)."""
    return _Lazy(lambda t: str(t).lower(), text)


def pct(value, digits=2, sign=False):
    """A percentage in the current language: 11.61% / 11,61 %."""
    text = f"{value:{'+' if sign else ''}.{digits}f}"
    return text.replace(".", ",") + "\u00a0%" if current_language() == "fr" else text + "%"


def month_name(month):
    """Name of a month (1–12) in the current language: November / novembre."""
    return _babel_date(date(2000, int(month), 1), "MMMM", locale=current_language())


def lazy_datetime(value, style="medium"):
    return _Lazy(fdatetime, value, style)
