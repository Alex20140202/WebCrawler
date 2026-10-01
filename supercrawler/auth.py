from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

Logger = Optional[Callable[[str], None]]

# Field names frameworks use for anti-CSRF tokens.
CSRF_FIELD_RE = re.compile(
    r"(csrfmiddlewaretoken|authenticity_token|_csrf|__csrf|csrf[_-]?token|"
    r"csrfparam|anti[_-]?forgery)", re.IGNORECASE
)

# Field names that mean "this is a second factor prompt".
TOTP_FIELD_RE = re.compile(
    r"(otp|2fa|two[_-]?factor|totp|mfa|one[_-]?time|verification[_-]?code|"
    r"auth[_-]?code|token[_-]?code|security[_-]?code)", re.IGNORECASE
)

CAPTCHA_HINT_RE = re.compile(
    r"(recaptcha|hcaptcha|g-recaptcha|h-captcha|captcha|g-recaptcha-response|"
    r"cf-turnstile|hc-captcha)", re.IGNORECASE
)

FAILURE_HINT_RE = re.compile(
    r"(incorrect|invalid\s+(?:password|username|credentials|login)|"
    r"authentication\s+(?:failed|error)|login\s+failed|wrong\s+password|"
    r"bad\s+credentials|access\s+denied|unauthorized|"
    r"用户名或密码|密码错误|登录失败|账号或密码)", re.IGNORECASE
)

USERNAME_FIELDS = ("username", "user", "login", "email", "userid", "user_id",
                   "account", "identifier", "j_username", "usr", "id")
PASSWORD_FIELDS = ("password", "passwd", "pass", "pwd", "secret", "j_password")

# Hidden fields we always replay back, whatever they are called.
GENERIC_HIDDEN_RE = re.compile(
    r"(csrf|token|authenticity|state|nonce|redirect|return|next|utm_)", re.IGNORECASE
)

REDACTED = "***"


class AuthError(Exception):
    """Login could not be completed. Never carries the password in its message."""


class LoginResult:
    __slots__ = ("ok", "method", "reason", "url", "needs_totp", "needs_captcha")

    def __init__(self, ok: bool, method: str, reason: str = "", url: str = "",
                 needs_totp: bool = False, needs_captcha: bool = False):
        self.ok = ok
        self.method = method
        self.reason = reason
        self.url = url
        self.needs_totp = needs_totp
        self.needs_captcha = needs_captcha

    def as_dict(self) -> Dict:
        return {"ok": self.ok, "method": self.method, "reason": self.reason,
                "needs_totp": self.needs_totp, "needs_captcha": self.needs_captcha}


def redact(text: str) -> str:
    """Blank out anything that looks like a password in a string we might log."""
    if not text:
        return text
    cleaned = re.sub(
        r"(?i)((?:password|passwd|pwd|secret|token|code)\s*[=:]\s*)(\S+)",
        r"\1%s" % REDACTED, text)
    return cleaned


def find_login_form(html: str, page_url: str) -> Optional[Dict]:
    """Pick the form most likely to be the login form."""
    soup = BeautifulSoup(html, "html.parser")
    best = None
    best_score = 0

    for form in soup.find_all("form"):
        method = str(form.get("method") or "get").lower()
        fields = {}
        for element in form.find_all(["input", "textarea"]):
            name = element.get("name")
            if not name:
                continue
            field_type = str(element.get("type") or "text").lower()
            fields[name] = field_type

        has_password = any(
            field_type == "password" or any(p in name.lower() for p in PASSWORD_FIELDS)
            for name, field_type in fields.items()
        )
        score = 0
        if has_password:
            score += 5
        if method == "post":
            score += 2
        if any("user" in n.lower() or "email" in n.lower() or n.lower() in USERNAME_FIELDS
               for n in fields):
            score += 2
        haystack = " ".join([
            " ".join(form.get("class") or []), str(form.get("id") or ""),
            form.get_text(" ", strip=True)[:200],
        ]).lower()
        if re.search(r"(login|log\s*in|sign\s*in|signin|authenticate|登录)", haystack):
            score += 3

        if has_password and score > best_score:
            action = form.get("action") or page_url
            best = {
                "action": urljoin(page_url, action),
                "method": method,
                "fields": fields,
                "csrf": _csrf_fields(form),
                "hidden": _hidden_fields(form),
            }
            best_score = score
    return best


def _csrf_fields(form) -> Dict[str, str]:
    out = {}
    for element in form.find_all("input"):
        name = element.get("name")
        if not name:
            continue
        field_type = str(element.get("type") or "").lower()
        value = element.get("value") or ""
        if field_type == "hidden" and (CSRF_FIELD_RE.search(name) or value):
            out[name] = value
    return out


def _hidden_fields(form) -> Dict[str, str]:
    out = {}
    for element in form.find_all("input"):
        if str(element.get("type") or "").lower() != "hidden":
            continue
        name = element.get("name")
        if name and GENERIC_HIDDEN_RE.search(name):
            out[name] = element.get("value") or ""
    return out


def pick_field(names: Dict[str, str], candidates: Sequence[str]) -> Optional[str]:
    """Choose the best-matching field name from candidates.

    Exact names win. Otherwise we score each field by how many candidate words
    it contains, so `user_email`, `login` and `j_username` all resolve against
    the same candidate list instead of needing one literal name each.
    """
    for candidate in candidates:
        if candidate in names:
            return candidate

    best = None
    best_score = 0
    for name in names:
        lowered = name.lower()
        score = sum(1 for candidate in candidates
                    if candidate in lowered or lowered in candidate)
        if score > best_score:
            best = name
            best_score = score
    return best


def needs_totp(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["input", "form"]):
        haystack = " ".join([
            element.get("name") or "", element.get("id") or "",
            " ".join(element.get("class") or []), str(element.get("placeholder") or ""),
            element.get_text(" ", strip=True)[:120] if element.name == "form" else "",
        ])
        if TOTP_FIELD_RE.search(haystack):
            return True
    return bool(TOTP_FIELD_RE.search(html)) and 'name="code"' in html.lower()


def has_captcha(html: str) -> bool:
    if CAPTCHA_HINT_RE.search(html):
        return True
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["iframe", "div", "img"]):
        marker = " ".join([
            str(element.get("class") or ""), str(element.get("id") or ""),
            str(element.get("src") or ""),
        ]).lower()
        if "captcha" in marker or "turnstile" in marker:
            return True
    return False


def find_code_form(html: str, page_url: str) -> Optional[Dict]:
    """Find a form asking for a one-time / verification code.

    Separate from find_login_form because a 2FA step has no password field,
    so reusing the login finder would never match.
    """
    soup = BeautifulSoup(html, "html.parser")
    best = None
    best_score = 0
    for form in soup.find_all("form"):
        fields = {}
        for element in form.find_all(["input", "textarea"]):
            name = element.get("name")
            if name:
                fields[name] = str(element.get("type") or "text").lower()
        if not fields:
            continue
        code_field = pick_field(fields, ("code", "otp", "token", "answer",
                                         "verification", "auth"))
        if not code_field or fields.get(code_field) == "password":
            continue
        haystack = " ".join([
            " ".join(form.get("class") or []), str(form.get("id") or ""),
            form.get_text(" ", strip=True)[:200],
        ])
        score = 1
        if TOTP_FIELD_RE.search(haystack):
            score += 3
        if score > best_score:
            action = form.get("action") or page_url
            best = {
                "action": urljoin(page_url, action),
                "method": str(form.get("method") or "post").lower(),
                "fields": fields,
                "code_field": code_field,
                "csrf": _csrf_fields(form),
                "hidden": _hidden_fields(form),
            }
            best_score = score
    return best


def looks_logged_in(html: str, final_url: str, login_url: str) -> bool:
    """Cheap check that we are no longer sitting on a login form."""
    if not html:
        return False
    if needs_totp(html):
        # A verification prompt is not a logged-in page.
        return False
    lowered = final_url.lower()
    if urlsplit(login_url).path.lower() not in ("", "/") and \
            urlsplit(login_url).path.lower() in lowered:
        if not re.search(r"(logout|signout|dashboard|home|account)", lowered):
            return False
    if FAILURE_HINT_RE.search(html[:4000]):
        return False
    form = find_login_form(html, final_url)
    if form is not None:
        return False
    return True


def save_cookies(session, path: str) -> Optional[str]:
    """Persist a session so later runs skip the login form entirely."""
    if not path:
        return None
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump({"cookies": payload}, fh, ensure_ascii=False)
    return path


def load_cookies(session, path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return 0
    count = 0
    for item in payload.get("cookies", []):
        try:
            session.cookies.set(
                item["name"], item["value"],
                domain=item.get("domain", ""), path=item.get("path", "/"),
            )
            count += 1
        except (KeyError, ValueError):
            continue
    return count


class Authenticator:
    """Signs in to a site the user is authorized to use.

    Scope is deliberately narrow: it posts the credentials it is given to the
    URL it is given, reads back the session the site hands out, and nothing
    else. It does not guess credentials, solve captchas, or reuse cookies
    belonging to anyone else.
    """

    def __init__(self, session, config, logger: Logger = None,
                 prompt_code: Optional[Callable[[str], str]] = None):
        self.session = session
        self.config = config
        self.logger = logger
        self.prompt_code = prompt_code

    def _log(self, message: str) -> None:
        if self.logger and getattr(self.config, "verbose", True):
            self.logger(redact(message))

    @property
    def enabled(self) -> bool:
        # A session file on its own means "reuse a session if one exists", not
        # "we hold credentials". Only an explicit login target or auth header
        # means there is something to authenticate with.
        return bool(self.config.login_url or self.config.auth_header)

    def password(self) -> str:
        """Resolve the password: an explicit inline value wins over the env var.

        Inline is only a fallback for people driving the library; the CLI
        prefers --password-env. We log which one was used so a surprise is
        visible in the output rather than silently changing behaviour.
        """
        if self.config.login_password:
            if os.environ.get(self.config.password_env_name):
                self._log("using the inline password and ignoring %s"
                          % self.config.password_env_name)
            return self.config.login_password
        return os.environ.get(self.config.password_env_name, "")

    def prepare(self) -> Dict:
        """Apply a saved session and/or an auth header before crawling."""
        info = {"session_restored": 0, "header": False}

        if self.config.session_file:
            restored = load_cookies(self.session, self.config.session_file)
            info["session_restored"] = restored
            if restored:
                self._log("restored %d cookie(s) from %s" % (restored,
                                                             self.config.session_file))

        if self.config.auth_header:
            name, value = _split_header(self.config.auth_header)
            if name and value:
                self.session.headers[name] = value
                info["header"] = True
                self._log("using auth header %s: %s" % (name, REDACTED))
        return info

    def login(self) -> LoginResult:
        if not self.config.login_url:
            return LoginResult(True, "none", "no login configured")

        url = self.config.login_url
        username = self.config.login_username
        password = self.password()

        if not username or not password:
            self._log("login configured but credentials are missing "
                      "(set %s in the environment)" % self.config.password_env_name)
            return LoginResult(False, "form", "missing credentials")

        try:
            page = self.session.get(url, timeout=self.config.timeout)
        except Exception as exc:
            return LoginResult(False, "form",
                               "could not load login page: %s" % type(exc).__name__)

        if has_captcha(page.text):
            self._log("login page has a captcha; refusing to solve it")
            return LoginResult(False, "form", "captcha present", url,
                               needs_captcha=True)

        form = find_login_form(page.text, page.url)
        if form is None:
            return LoginResult(False, "form", "no login form found", url)

        user_field = pick_field(form["fields"], USERNAME_FIELDS)
        pass_field = pick_field(form["fields"], PASSWORD_FIELDS)
        if not user_field or not pass_field:
            return LoginResult(False, "form",
                               "could not identify username/password fields", url)

        data: Dict[str, str] = {}
        data.update(form["csrf"])
        data.update(form["hidden"])
        data[user_field] = username
        data[pass_field] = password

        self._log("submitting login form at %s (user field %r, pass field %r)"
                  % (url, user_field, pass_field))

        try:
            response = self.session.post(form["action"], data=data,
                                         timeout=self.config.timeout,
                                         allow_redirects=True)
        except Exception as exc:
            return LoginResult(False, "form",
                               "login request failed: %s" % type(exc).__name__, url)

        if needs_totp(response.text):
            return self._handle_second_factor(response, url)
        if needs_totp(page.text) and not looks_logged_in(response.text,
                                                         response.url, url):
            return self._handle_second_factor(response, url)

        if not looks_logged_in(response.text, response.url, url):
            reason = "credentials rejected" if FAILURE_HINT_RE.search(
                response.text[:4000]) else "still on the login page after submitting"
            self._log("login failed: %s" % reason)
            return LoginResult(False, "form", reason, url)

        if self.config.session_file:
            save_cookies(self.session, self.config.session_file)
            self._log("saved session to %s" % self.config.session_file)

        self._log("logged in to %s" % response.url)
        return LoginResult(True, "form", "", response.url)

    def _handle_second_factor(self, response, url: str) -> LoginResult:
        """Ask the user for their own 2FA code rather than trying to work around it."""
        code = None
        if self.prompt_code is not None:
            try:
                code = (self.prompt_code("Two-factor code for %s: " % url) or "").strip()
            except (EOFError, KeyboardInterrupt):
                code = ""
        if not code:
            return LoginResult(False, "form", "second factor required", url,
                               needs_totp=True)

        form = find_code_form(response.text, response.url)
        if form is None:
            return LoginResult(False, "form", "no 2FA form found", url,
                               needs_totp=True)

        data = dict(form["csrf"])
        data.update(form["hidden"])
        data[form["code_field"]] = code

        self._log("submitting second-factor code")
        try:
            final = self.session.post(form["action"], data=data,
                                      timeout=self.config.timeout,
                                      allow_redirects=True)
        except Exception as exc:
            return LoginResult(False, "form",
                               "2FA request failed: %s" % type(exc).__name__, url,
                               needs_totp=True)

        if not looks_logged_in(final.text, final.url, url):
            return LoginResult(False, "form", "second factor rejected", url,
                               needs_totp=True)

        if self.config.session_file:
            save_cookies(self.session, self.config.session_file)
        self._log("logged in after second factor")
        return LoginResult(True, "form+totp", "", final.url)


def _split_header(raw: str) -> Tuple[str, str]:
    if ":" in raw:
        name, value = raw.split(":", 1)
        return name.strip(), value.strip()
    if "=" in raw:
        name, value = raw.split("=", 1)
        return name.strip(), value.strip()
    return "", ""


def mask(value: str, keep: int = 4) -> str:
    """Show enough of a secret to recognize it, never enough to use it."""
    if not value:
        return "(empty)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return "%s...%s (%d chars)" % (value[:keep], value[-keep:], len(value))


def _b64url_decode(segment: str) -> Optional[bytes]:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, binascii.Error):
        return None


def inspect_token(raw: str) -> Dict:
    """Describe an auth header locally: shape, and JWT claims if it is one.

    Decodes only the token's own payload, which is not encrypted. Nothing is
    sent anywhere and nothing is written to disk.
    """
    info = {
        "valid_header": False,
        "name": "",
        "scheme": "",
        "value_preview": "",
        "looks_like_jwt": False,
        "claims": {},
        "expired": None,
        "expires_in": None,
        "not_yet_valid": False,
        "notes": [],
    }
    if not raw or not raw.strip():
        info["notes"].append("empty")
        return info

    name, value = _split_header(raw)
    if not name or not value:
        info["notes"].append(
            "expected 'Name: value' or 'Name=value', got a bare value"
        )
        info["value_preview"] = mask(value or raw.strip())
        return info

    info["valid_header"] = True
    info["name"] = name
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in (
            "bearer", "token", "basic", "apikey", "digest"):
        info["scheme"] = parts[0]
        token = parts[1]
    else:
        token = value
    info["value_preview"] = mask(token)

    header_name = name.lower()
    if header_name == "authorization" and not info["scheme"]:
        info["notes"].append(
            "Authorization usually needs a scheme, e.g. 'Authorization: Bearer <token>'"
        )

    segments = token.split(".")
    if len(segments) != 3:
        if len(segments) > 1:
            info["notes"].append("looks like a JWT but has %d parts, expected 3"
                                 % len(segments))
        return info

    info["looks_like_jwt"] = True
    payload = _b64url_decode(segments[1])
    if payload is None:
        info["notes"].append("payload segment is not valid base64url")
        return info
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        info["notes"].append("payload is not valid JSON")
        return info
    if not isinstance(claims, dict):
        info["notes"].append("payload is not a JSON object")
        return info

    info["claims"] = {
        key: claims[key]
        for key in ("iss", "sub", "aud", "exp", "iat", "nbf", "scope", "scp")
        if key in claims
    }
    now = int(time.time())
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        info["expired"] = now > exp
        info["expires_in"] = int(exp) - now
    else:
        info["notes"].append("no exp claim; this token may never expire on its own")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now < nbf:
        info["not_yet_valid"] = True
        info["notes"].append("not valid yet (nbf is in the future)")
    return info


def describe_token(raw: str) -> str:
    """Human-readable summary for the CLI."""
    info = inspect_token(raw)
    lines = []
    if not info["valid_header"]:
        lines.append("  header        : INVALID (%s)" % info["notes"][0])
        return "\n".join(lines)

    lines.append("  header name   : %s" % info["name"])
    lines.append("  scheme        : %s" % (info["scheme"] or "(none)"))
    lines.append("  value         : %s" % info["value_preview"])
    lines.append("  format        : %s"
                 % ("JWT" if info["looks_like_jwt"] else "opaque"))

    if info["looks_like_jwt"] and info["claims"]:
        for key, value in info["claims"].items():
            if key in ("exp", "iat", "nbf"):
                try:
                    stamp = datetime.fromtimestamp(int(value), timezone.utc)
                    rendered = stamp.strftime("%Y-%m-%d %H:%M:%SZ")
                except (ValueError, OSError, OverflowError):
                    rendered = str(value)
                lines.append("  %-13s : %s" % (key, rendered))
            else:
                lines.append("  %-13s : %s" % (key, value))
    if info["expired"] is True:
        lines.append("  status        : EXPIRED")
    elif info.get("not_yet_valid"):
        lines.append("  status        : not valid yet")
    elif info["expires_in"] is not None:
        lines.append("  status        : valid, expires in %s"
                     % _humanize_seconds(info["expires_in"]))
    for note in info["notes"]:
        lines.append("  note          : %s" % note)
    if info["valid_header"] and info["expired"] is not True \
            and not info.get("not_yet_valid") and info["expires_in"] is None \
            and info["looks_like_jwt"]:
        lines.append("  status        : expiry unknown until the server "
                     "rejects it")
    lines.append("")
    lines.append("  This only inspected the token locally. Nothing was sent anywhere.")
    return "\n".join(lines)


def _humanize_seconds(seconds: int) -> str:
    if seconds < 0:
        return "already expired"
    if seconds < 90:
        return "%d seconds" % seconds
    if seconds < 5400:
        return "%d minutes" % (seconds // 60)
    if seconds < 172800:
        return "%d hours" % (seconds // 3600)
    return "%d days" % (seconds // 86400)


def describe_auth(config) -> Dict:
    """A report-safe description: never includes the password."""
    return {
        "login_configured": bool(config.login_url),
        "login_url": config.login_url or "",
        "username": config.login_username or "",
        "password_source": ("inline" if config.login_password else
                            ("environment:%s" % config.login_password_env)
                            if config.login_password_env else "none"),
        "session_file": config.session_file or "",
        "auth_header": bool(config.auth_header),
    }


def hosts_match(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()