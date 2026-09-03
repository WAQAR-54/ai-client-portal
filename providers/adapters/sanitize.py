def sanitize_error(message: str, api_key: str | None) -> str:
    """Strips a raw API key out of an exception message before it's ever
    stored in Provider.last_sync_error or logged - defense in depth in
    case an SDK's exception repr ever includes request headers/params."""
    if api_key and api_key in message:
        message = message.replace(api_key, "[REDACTED]")
    return message
