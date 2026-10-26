"""Build the deterministic sample target repository used by the demo.

Creates a fresh git repository with a ``main`` branch and three seeded PR
branches. Every commit uses a fixed author/committer date so repeated builds are
byte-for-byte reproducible.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEST = Path(__file__).resolve().parent / ".build" / "target_repo"

COMMIT_DATE = "2026-01-01T00:00:00+0000"

BRANCH_SIGNATURE_BREAK = "pr/signature-break"
BRANCH_UNTESTED_ENDPOINT = "pr/untested-endpoint"
BRANCH_DOCS_DRIFT = "pr/docs-drift"

SERVICES_BASE = '''"""Business services for the sample application."""

from __future__ import annotations

from .models import User


def create_user(name: str, email: str) -> User:
    return User(name=name, email=email)


def calculate_discount(amount: int, percent: int) -> int:
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return amount - (amount * percent // 100)


def send_invoice(customer_id: str, total: int) -> str:
    if not customer_id:
        raise ValueError("customer_id is required")
    return f"invoice:{customer_id}:{total}"
'''

SERVICES_TENANT = SERVICES_BASE.replace(
    "def create_user(name: str, email: str) -> User:",
    "def create_user(name: str, email: str, tenant_id: str) -> User:",
)

SERVICES_RENAMED = SERVICES_BASE.replace(
    "def calculate_discount(amount: int, percent: int) -> int:",
    "def calculate_discount(amount_cents: int, percent: int) -> int:",
).replace(
    "    return amount - (amount * percent // 100)",
    "    return amount_cents - (amount_cents * percent // 100)",
)

SERVICES_REFUND = SERVICES_BASE + '''

def refund_payment(charge_id: str, amount_cents: int) -> str:
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    return f"refund:{charge_id}:{amount_cents}"
'''

MODELS = '''"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    name: str
    email: str
'''

WEB = '''"""Tiny router shim so the fixture runs without third-party packages."""

from __future__ import annotations

from typing import Callable


class Router:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.routes: list[tuple[str, str]] = []

    def _register(self, method: str, path: str) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.routes.append((method, self.prefix + path))
            return handler

        return decorator

    def get(self, path: str, **kwargs: object) -> Callable:
        return self._register("GET", path)

    def post(self, path: str, **kwargs: object) -> Callable:
        return self._register("POST", path)

    def patch(self, path: str, **kwargs: object) -> Callable:
        return self._register("PATCH", path)


router = Router()
'''

API_BASE = '''"""HTTP handlers for the sample application."""

from __future__ import annotations

from .services import calculate_discount, send_invoice
from .web import router


@router.get("/discounts/{code}")
def get_discount(code: str, amount: int = 1000) -> dict:
    value = calculate_discount(amount, 10)
    return {"code": code, "discount": value}


@router.get("/invoices/{customer_id}")
def get_invoice(customer_id: str) -> dict:
    reference = send_invoice(customer_id, 2500)
    return {"customer_id": customer_id, "invoice": reference}
'''

API_ROUTE_DRIFT = API_BASE.replace(
    '@router.get("/discounts/{code}")',
    '@router.get("/promotions/{code}")',
)

API_REFUND = API_BASE.replace(
    "from .services import calculate_discount, send_invoice",
    "from .services import calculate_discount, refund_payment, send_invoice",
) + '''

@router.patch("/charges/{charge_id}/refund")
def refund_charge(charge_id: str, amount_cents: int) -> dict:
    reference = refund_payment(charge_id, amount_cents)
    return {"charge_id": charge_id, "refund": reference}
'''

CLIENTS = '''"""Consumers that would silently break if create_user changes."""

from __future__ import annotations

from .services import create_user


def create_owner(name: str, email: str):
    return create_user(name, email)


def invite_member(name: str, email: str):
    return create_user(name, email)
'''

ADMIN = '''"""Admin-side consumers of the service layer."""

from __future__ import annotations

from .services import create_user


def seed_admin(name: str, email: str):
    return create_user(name, email)
'''

INIT = '''"""Sample application package."""
'''

TEST_SERVICES = '''import pytest

from sample_app.services import calculate_discount, send_invoice


def test_discount_applies_percentage():
    assert calculate_discount(2000, 15) == 1700


def test_discount_rejects_out_of_range_percent():
    with pytest.raises(ValueError):
        calculate_discount(100, 200)


def test_invoice_includes_customer_and_total():
    assert send_invoice("cust_1", 100) == "invoice:cust_1:100"


def test_invoice_requires_customer():
    with pytest.raises(ValueError):
        send_invoice("", 100)
'''

TEST_API = '''from sample_app.api import get_discount, get_invoice


def test_discount_handler():
    assert get_discount("SAVE", amount=1000) == {"code": "SAVE", "discount": 900}


def test_invoice_handler():
    assert get_invoice("cust_9")["invoice"] == "invoice:cust_9:2500"
'''

README = '''# Sample App

A tiny service layer used as the PR Impact Simulator target repository.

## Usage

```python
from sample_app.services import calculate_discount, send_invoice

total = calculate_discount(amount=2000, percent=15)
print(send_invoice("cust_1", total))
```

## API reference

- `GET /discounts/{code}` — look up a discount by code.
- `GET /invoices/{customer_id}` — return an invoice reference.

## Tests

Run `python -m pytest`.
'''

GITIGNORE = '''__pycache__/
.pytest_cache/
.coverage
coverage.json
'''


@dataclass(frozen=True)
class Fixture:
    repo: Path
    branches: dict[str, str]


def build_fixture(dest: str | Path = DEFAULT_DEST, force: bool = False) -> Fixture:
    destination = Path(dest)
    branches = {
        "signature-break": BRANCH_SIGNATURE_BREAK,
        "untested-endpoint": BRANCH_UNTESTED_ENDPOINT,
        "docs-drift": BRANCH_DOCS_DRIFT,
    }
    if destination.exists():
        if not force:
            return Fixture(destination, branches)
        _remove_tree(destination)
    destination.mkdir(parents=True)

    _git(destination, "init", "-b", "main")
    _git(destination, "config", "user.name", "Fixture Bot")
    _git(destination, "config", "user.email", "fixture@example.invalid")
    _git(destination, "config", "commit.gpgsign", "false")
    _git(destination, "config", "core.autocrlf", "false")

    _write(destination, ".gitignore", GITIGNORE)
    _write(destination, "README.md", README)
    _write(destination, "sample_app/__init__.py", INIT)
    _write(destination, "sample_app/models.py", MODELS)
    _write(destination, "sample_app/services.py", SERVICES_BASE)
    _write(destination, "sample_app/web.py", WEB)
    _write(destination, "sample_app/api.py", API_BASE)
    _write(destination, "sample_app/clients.py", CLIENTS)
    _write(destination, "sample_app/admin.py", ADMIN)
    _write(destination, "tests/test_services.py", TEST_SERVICES)
    _write(destination, "tests/test_api.py", TEST_API)
    _commit(destination, "seed sample service API")

    _checkout_new_branch(destination, BRANCH_SIGNATURE_BREAK)
    _write(destination, "sample_app/services.py", SERVICES_TENANT)
    _commit(destination, "add tenant scoping to create_user signature")

    _checkout_new_branch(destination, BRANCH_UNTESTED_ENDPOINT)
    _write(destination, "sample_app/services.py", SERVICES_REFUND)
    _write(destination, "sample_app/api.py", API_REFUND)
    _commit(destination, "add refund endpoint without tests")

    _checkout_new_branch(destination, BRANCH_DOCS_DRIFT)
    _write(destination, "sample_app/services.py", SERVICES_RENAMED)
    _write(destination, "sample_app/api.py", API_ROUTE_DRIFT)
    _commit(destination, "rename discount units and move promotion route")

    _git(destination, "checkout", "main")
    return Fixture(destination, branches)


def _remove_tree(path: Path) -> None:
    """Remove a fixture checkout, tolerating Windows read-only git files."""

    def _on_error(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            raise

    shutil.rmtree(path, onerror=_on_error)


def _checkout_new_branch(repo: Path, branch: str) -> None:
    _git(repo, "checkout", "-b", branch, "main")


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_DATE": COMMIT_DATE,
            "GIT_COMMITTER_DATE": COMMIT_DATE,
            "GIT_AUTHOR_NAME": "Fixture Bot",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Bot",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


if __name__ == "__main__":
    fixture = build_fixture(force=True)
    print(f"built {fixture.repo}")
    for name, branch in fixture.branches.items():
        print(f"  {name}: {branch}")