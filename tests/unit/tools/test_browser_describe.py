"""A browser action's card says what it lands on, and whether it sends or spends (#31).

The policy used to see a click as `uid=n10`. Now the tool looks at the page first:
the URL and title, and the role, text and form of the element.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from approval.policy import ApprovalPolicy, Verdict
from approval.unattended import UnattendedPolicy, forbidden_reason
from config.approval_mode import ApprovalMode
from tools.universal.browser_describe import describe_browser_call

_APPLY_TREE = (
    'uid=n3 RootWebArea "Apply to Acme"\n'
    "  uid=n1 form\n"
    '    uid=n8 LabelText "Name "\n'
    '      uid=n2 textbox "Name"\n'
    '    uid=n10 button "Submit application"\n'
    '    uid=n11 link "Next"\n'
    'uid=n20 button "Log out"\n'
)


@dataclass
class _Result:
    stdout: str


class _Page:
    """Answers chrome-agent's `eval` and `inspect` the way it does, for one page."""

    def __init__(self, url: str, *, target=None, focused=None, tree: str = _APPLY_TREE, title: str = "Apply to Acme"):
        self.page = {"url": url, "title": title, "target": target, "focused": focused}
        self.tree = tree
        self.commands: list[list[str]] = []

    async def __call__(self, command: list[str], timeout: int) -> _Result:
        self.commands.append(command)
        if "eval" in command:
            return _Result(json.dumps({"ok": True, "result": json.dumps(self.page)}))
        return _Result(json.dumps({"ok": True, "snapshot": self.tree}))


async def _unreadable(command: list[str], timeout: int) -> _Result:
    raise RuntimeError("no browser")


def _describe(params: dict, page) -> object:
    return describe_browser_call(params, ["chrome-agent", "--json", "--browser", "t1"], page)


async def test_a_submit_shows_the_url_and_button_text_and_is_marked_as_sending() -> None:
    page = _Page("https://jobs.acme.test/apply/7?session=abc")

    request = await _describe({"action": "click", "uid": "n10"}, page)

    assert request.title == 'Browser - Click "Submit application"'
    assert "https://jobs.acme.test/apply/7" in request.message
    assert "Apply to Acme" in request.message
    assert "(button, in a form)" in request.message
    assert request.action.reaches_third_party and not request.action.spends_money


@pytest.mark.parametrize("mode", [ApprovalMode.ASK, ApprovalMode.SAFE])
async def test_a_submit_is_never_approved_without_you(mode: ApprovalMode) -> None:
    request = await _describe({"action": "click", "uid": "n10"}, _Page("https://jobs.acme.test/apply/7"))
    policy = ApprovalPolicy(mode_provider=lambda: mode, unattended=UnattendedPolicy())

    assert (await policy.rule(request.action)).verdict is Verdict.ASK
    assert forbidden_reason(request.action)


async def test_a_plain_link_is_not_marked_as_sending() -> None:
    request = await _describe({"action": "click", "uid": "n11"}, _Page("https://jobs.acme.test/apply/7"))

    assert request.title == 'Browser - Click "Next"'
    assert not request.action.reaches_third_party


async def test_a_button_outside_any_form_sends_only_if_its_words_say_so() -> None:
    request = await _describe({"action": "click", "uid": "n20"}, _Page("https://jobs.acme.test/apply/7"))

    assert "(button)" in request.message
    assert not request.action.reaches_third_party


async def test_pressing_a_button_at_checkout_spends_money() -> None:
    target = {"role": "button", "text": "Continue", "inForm": True}
    page = _Page("https://shop.test/checkout/step-2", target=target, title="Checkout")

    request = await _describe({"action": "click", "selector": "form button"}, page)

    assert request.action.spends_money
    assert "This may spend money." in request.message


@pytest.mark.parametrize(("key", "sends"), [("Enter", True), ("Tab", False)])
async def test_enter_in_a_form_field_submits_the_form(key: str, sends: bool) -> None:
    focused = {"role": "input", "text": "Name", "inForm": True}

    request = await _describe(
        {"action": "press", "value": key}, _Page("https://jobs.acme.test/apply/7", focused=focused)
    )

    assert request.title == f'Browser - Press {key} in "Name"'
    assert request.action.reaches_third_party is sends


async def test_typing_is_not_sending_and_the_card_shows_the_value() -> None:
    request = await _describe({"action": "fill", "uid": "n2", "value": "Ada"}, _Page("https://jobs.acme.test/apply/7"))

    assert 'with "Ada"' in request.message
    assert not request.action.reaches_third_party


async def test_a_page_north_cannot_read_still_asks_and_says_so() -> None:
    request = await _describe({"action": "click", "uid": "n10"}, _unreadable)

    assert request is not None
    assert request.title == "Browser - Click n10"
    assert "could not read the page" in request.message


async def test_the_same_button_on_the_same_page_is_the_same_action_whatever_the_query() -> None:
    """So a decision you took replays - but only for that button on that page."""
    first = await _describe({"action": "click", "uid": "n10"}, _Page("https://jobs.acme.test/apply/7?s=1"))
    again = await _describe({"action": "click", "uid": "n10"}, _Page("https://jobs.acme.test/apply/7?s=2"))
    other = await _describe({"action": "click", "uid": "n11"}, _Page("https://jobs.acme.test/apply/7?s=1"))

    assert first.action.describe() == again.action.describe()
    assert first.action.describe() != other.action.describe()


async def test_a_selector_reaches_the_page_script_as_a_literal() -> None:
    page = _Page("https://x.test/")
    selector = "button[aria-label=\"Send\"]'); alert(1); ('"

    await _describe({"action": "click", "selector": selector}, page)

    script = page.commands[0][-1]
    assert json.dumps(selector) in script


async def test_opening_a_page_in_your_own_browser_says_so_without_looking() -> None:
    page = _Page("https://x.test/")

    request = await _describe({"action": "goto", "url": "https://mail.test/", "connect": "auto"}, page)

    assert page.commands == []
    assert request.title == "Browser - Open https://mail.test/"
    assert "your own browser" in request.message
