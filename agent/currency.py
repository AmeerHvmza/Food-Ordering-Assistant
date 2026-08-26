"""Reply-text currency formatting. Pakistani rupees only."""


def sanitize_currency(text: str) -> str:
    """Replace a stray Indian rupee symbol before the reply reaches the user."""
    if not text:
        return text
    return text.replace("₹", "Rs.")
