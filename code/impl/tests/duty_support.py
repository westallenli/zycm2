"""Synthetic named-duty fixture for tests of older, unrelated workflows."""

from panda_brain.models import PersonRecord


DUTY_PHONE = "13999990001"


def duty_token(state, *, phone=None):
    phone = phone or DUTY_PHONE
    if phone not in state.registry.library:
        state.registry.import_person_library([
            PersonRecord(name="模拟值班员", phone=phone, unit="模拟值班室")
        ])
    state.configure_duty_subjects((phone,))
    token, error = state.member_login(phone, "123456")
    if error or not token:
        raise AssertionError(f"Cannot authenticate synthetic duty officer: {error}")
    return token


def duty_cookie(state, *, phone=None):
    return f"member_session={duty_token(state, phone=phone)}"
