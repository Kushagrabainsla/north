from validators import is_strong_password, is_valid_email


def validate_signup(email, password):
    return is_valid_email(email) and is_strong_password(password)
