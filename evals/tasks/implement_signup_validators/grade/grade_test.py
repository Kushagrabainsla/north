from signup import validate_signup
from validators import is_strong_password, is_valid_email


def test_valid_signup():
    assert validate_signup("a@b.com", "abcd1234") is True


def test_email_needs_at():
    assert validate_signup("abc.com", "abcd1234") is False


def test_email_needs_local_part():
    assert validate_signup("@b.com", "abcd1234") is False


def test_email_rejects_two_at():
    assert validate_signup("a@@b.com", "abcd1234") is False


def test_password_too_short():
    assert validate_signup("a@b.com", "ab12") is False


def test_password_needs_digit():
    assert validate_signup("a@b.com", "abcdefgh") is False


def test_email_helper_directly():
    assert is_valid_email("x@y.z") is True
    assert is_valid_email("xy.z") is False


def test_password_helper_directly():
    assert is_strong_password("abc12345") is True
    assert is_strong_password("abcdefgh") is False
