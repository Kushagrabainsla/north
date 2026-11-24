"""North knows which profile is signed in where, as a person remembers it.

It learns from what it saw (a sign-in page or not, and which of the profile's own names a
page shows) and from where you browse (Chrome's history, site names and counts only). When
a call names no profile and one answer stands out, north uses it and says so; otherwise
the call is refused and the agent asks you.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from config.browser_profiles import BrowserProfile
from config.strategy import NorthSettings
from tests.conftest import bind_approvals
from tools.models import ToolInput, ToolOutput
from tools.universal import browser_taskwindow as bridge
from tools.universal.browser import BrowserTool
from tools.universal.browser_accounts import BrowserAccounts, chrome_identity, history_sites

LINKEDIN = "https://www.linkedin.com/feed/"


def _profile(tmp_path: Path, profile_id: str, directory: str, enabled: bool = True) -> BrowserProfile:
    return BrowserProfile(
        id=profile_id,
        name=profile_id.title(),
        purpose="tests",
        context="existing",
        data_directory=str(tmp_path / "chrome"),
        profile_directory=directory,
        enabled=enabled,
    )


def _history(tmp_path: Path, directory: str, visits: dict[str, int]) -> None:
    folder = tmp_path / "chrome" / directory
    folder.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(folder / "History")
    connection.execute("CREATE TABLE urls (url TEXT, visit_count INTEGER)")
    connection.executemany("INSERT INTO urls VALUES (?, ?)", list(visits.items()))
    connection.commit()
    connection.close()


def test_seen_signed_in_in_one_profile_is_the_pick_with_its_account(tmp_path) -> None:
    school, personal = _profile(tmp_path, "school", "Profile 3"), _profile(tmp_path, "personal", "Default")
    accounts = BrowserAccounts(tmp_path / "accounts.db")
    accounts.record(LINKEDIN, "school", signed_in=True, account="Ada Lovelace")
    accounts.record("https://linkedin.com/in/someone/", "personal", signed_in=False)

    pick = accounts.pick("https://linkedin.com/jobs/", [school, personal])

    assert pick is not None and pick.profile_id == "school"
    assert "signed in on linkedin.com as Ada Lovelace" in pick.why
    assert accounts.signed_in_sites("school") == ["linkedin.com as Ada Lovelace"]
    accounts.record(LINKEDIN, "school", signed_in=True)  # a page that shows no name keeps the account
    assert accounts.signed_in_sites("school") == ["linkedin.com as Ada Lovelace"]


def test_signed_in_in_two_profiles_or_seen_nowhere_is_no_pick(tmp_path) -> None:
    school, personal = _profile(tmp_path, "school", "Profile 3"), _profile(tmp_path, "personal", "Default")
    accounts = BrowserAccounts(tmp_path / "accounts.db")
    assert accounts.pick(LINKEDIN, [school, personal]) is None
    accounts.record(LINKEDIN, "school", signed_in=True)
    accounts.record(LINKEDIN, "personal", signed_in=True)
    assert accounts.pick(LINKEDIN, [school, personal]) is None  # which account is yours to say
    assert accounts.pick(LINKEDIN, [school, _profile(tmp_path, "personal", "Default", enabled=False)]) is not None


def test_history_picks_where_nearly_all_visits_are_and_never_reaches_a_model(tmp_path) -> None:
    school, personal = _profile(tmp_path, "school", "Profile 3"), _profile(tmp_path, "personal", "Default")
    _history(tmp_path, "Profile 3", {"https://www.linkedin.com/feed/": 40, "https://mail.google.com/": 2})
    _history(tmp_path, "Default", {"https://linkedin.com/in/x/": 1, "https://mail.google.com/mail/": 30})
    accounts = BrowserAccounts(tmp_path / "accounts.db")

    assert history_sites(school)["linkedin.com"] == 40
    assert accounts.refresh_history([school, personal])
    assert not accounts.refresh_history([school, personal])  # at most once a day

    assert accounts.pick(LINKEDIN, [school, personal]).profile_id == "school"
    assert accounts.pick("https://mail.google.com/", [school, personal]).profile_id == "personal"
    assert accounts.signed_in_sites("school") == []  # history is never listed for the model
    accounts.record(LINKEDIN, "school", signed_in=False)
    assert accounts.pick(LINKEDIN, [school, personal]) is None  # north saw it asking to sign in there


def test_the_profile_s_names_come_from_chrome_s_own_record(tmp_path) -> None:
    (tmp_path / "chrome").mkdir()
    state = {"profile": {"info_cache": {"Profile 3": {"gaia_name": "Ada Lovelace", "user_name": "ada@school.edu"}}}}
    (tmp_path / "chrome" / "Local State").write_text(json.dumps(state))
    assert chrome_identity(_profile(tmp_path, "school", "Profile 3")) == ["Ada Lovelace", "ada@school.edu"]
    assert chrome_identity(_profile(tmp_path, "personal", "Default")) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize(
    ("path", "password", "text", "expected"),
    [
        ("/feed/", False, "Home Ada Lovelace Messaging", {"signin_page": False, "account": "Ada Lovelace"}),
        ("/login", False, "Sign in", {"signin_page": True, "account": ""}),
        ("/feed/", True, "Email Password", {"signin_page": True, "account": ""}),
    ],
)
def test_a_page_says_whether_it_asks_to_sign_in_and_whose_name_it_shows(path, password, text, expected) -> None:
    page = (
        f"globalThis.location = {{href: 'https://x.test{path}', pathname: {json.dumps(path)}}};"
        f"globalThis.document = {{title: 'T', body: {{innerText: {json.dumps(text)}}},"
        f" querySelector: () => {'({})' if password else 'null'}, querySelectorAll: () => []}};"
    )
    script = bridge._PAGE_STATE_SCRIPT % {"names": json.dumps(["Ada Lovelace", "ada@school.edu"])}
    done = subprocess.run(
        ["node", "-e", "eval(process.argv[1]); console.log(eval(process.argv[2]))", page, script],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    result = json.loads(done.stdout)
    assert {key: result[key] for key in expected} == expected


@pytest.mark.usefixtures("chrome_installed")
async def test_a_call_that_names_no_profile_uses_the_one_signed_in_and_says_so(tmp_path, monkeypatch) -> None:
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    )
    school, personal = _profile(tmp_path, "school", "Profile 3"), _profile(tmp_path, "personal", "Default")
    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles([school, personal])
    accounts = BrowserAccounts(tmp_path / "accounts.db")
    accounts.record(LINKEDIN, "school", signed_in=True, account="Ada Lovelace")
    native = Mock()
    page = {"action": "goto", "url": LINKEDIN, "signin_page": False, "account": "Ada Lovelace"}
    native.call = AsyncMock(return_value=ToolOutput(success=True, data=page))
    native.verify = AsyncMock(side_effect=RuntimeError("no page to look at in a test"))
    store = ApprovalStore()
    tool = bind_approvals(
        BrowserTool(north_settings=settings, existing_browser=native, accounts=accounts), ApprovalMode.ASK, store=store
    )

    running = asyncio.create_task(tool.execute(ToolInput(params={"action": "goto", "url": LINKEDIN, "task_id": "t"})))
    while not store.pending():
        await asyncio.sleep(0.01)
    [card] = store.pending()
    assert card.title == "Browser: School"  # the card names the picked profile
    store.resolve(card.id, "approved", decided_by="you")
    output = await running

    assert native.call.await_args.args[0].id == "school"
    assert output.data["picked_profile"].startswith("School (school): signed in on linkedin.com as Ada Lovelace")

    # A site no profile is known on, in a task that has used no profile yet: north cannot pick, so it asks.
    # (Task "t" now keeps School, the profile it used.)
    call = ToolInput(params={"action": "goto", "url": "https://example.org/", "task_id": "fresh"})
    unknown = await tool.execute(call)
    assert not unknown.success and "Select profile_id" in unknown.error
