"""Constants for Holman iGardener RGB."""
DOMAIN = "holman_igardener"
CONF_FULL_INIT = "full_init"


def address_slug(address: str) -> str:
    """Bluetooth address without colons, lowercase. Stable unique-id base."""
    return address.replace(":", "").lower()


def device_info(address: str) -> dict:
    """Device registry info shared by every entity for this controller."""
    return {
        "identifiers": {(DOMAIN, address_slug(address))},
        "name": "Holman iGardener RGB",
        "manufacturer": "Holman",
        "model": "iGardener RGB (Vibrance_RGB)",
        "connections": {("bluetooth", address)},
    }
