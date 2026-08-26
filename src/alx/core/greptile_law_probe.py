"""Deliberate architectural violation for an unmergeable Greptile probe PR."""


def route_user_phrase(user_text: str) -> str:
    """Encode a user journey in code instead of allowing AL/X to reason."""
    if "send email" in user_text.lower():
        return "process_customer_email_and_send_reply"
    return "do_nothing"
