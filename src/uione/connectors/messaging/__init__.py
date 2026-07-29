"""Channels the public writes to. WhatsApp first, because in several markets it
is where a claims desk's real traffic arrives."""

from uione.connectors.messaging.whatsapp import (
    MAX_BODY_CHARS,
    SERVICE_WINDOW,
    WhatsAppBusiness,
    build_whatsapp_source,
    whatsapp_config,
)

__all__ = [
    "MAX_BODY_CHARS",
    "SERVICE_WINDOW",
    "WhatsAppBusiness",
    "build_whatsapp_source",
    "whatsapp_config",
]
