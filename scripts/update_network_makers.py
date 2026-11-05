"""Rebuild resources/network-makers.json.gz: who made each network card, by the first half of its address.

The source is the IEEE registry of hardware-address prefixes, as published in the oui-data
package (BSD-2-Clause): https://github.com/silverwind/oui-data. Run it with that package's
index.json when the registry has changed enough to matter:

    python scripts/update_network_makers.py path/to/index.json

Each maker is kept as its short name ("TP-Link", not "TP-LINK TECHNOLOGIES CO.,LTD."), the way
the Devices page shows it.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "resources" / "network-makers.json.gz"

# The makers a home network is full of, by the name they are known by.
_KNOWN = {
    "tp-link": "TP-Link",
    "espressif": "Espressif",
    "apple": "Apple",
    "philips lighting": "Philips Hue (Signify)",
    "signify": "Philips Hue (Signify)",
    "lifi labs": "LIFX",
    "wiz iot": "WiZ",
    "tuya": "Tuya",
    "shelly": "Shelly",
    "allterco": "Shelly",
    "belkin": "Belkin",
    "amazon": "Amazon",
    "google": "Google",
    "nest labs": "Google Nest",
    "sonos": "Sonos",
    "samsung": "Samsung",
    "lg electronics": "LG",
    "roku": "Roku",
    "xiaomi": "Xiaomi",
    "yeelink": "Yeelight",
    "nanoleaf": "Nanoleaf",
    "meross": "Meross",
    "broadlink": "BroadLink",
    "ecobee": "ecobee",
    "resideo": "Resideo (Honeywell Home)",
    "honeywell": "Honeywell",
    "lutron": "Lutron",
    "leviton": "Leviton",
    "wyze": "Wyze",
    "ring": "Ring",
    "netgear": "Netgear",
    "ubiquiti": "Ubiquiti",
    "eero": "eero",
    "asustek": "ASUS",
    "raspberry pi": "Raspberry Pi",
    "intel": "Intel",
    "microsoft": "Microsoft",
    "sony": "Sony",
    "nintendo": "Nintendo",
    "hewlett packard": "HP",
    "dell": "Dell",
    "intellirocks": "Govee",
    "murata": "Murata",
    "texas instruments": "Texas Instruments",
    "silicon labs": "Silicon Labs",
    "realtek": "Realtek",
    "azurewave": "AzureWave",
    "hon hai": "Foxconn",
}
_SUFFIX = re.compile(
    r"[,\s]+(inc\.?|incorporated|co\.?,?\s*ltd\.?|company limited|co\.?|corp\.?|corporation|limited|ltd\.?|llc|"
    r"gmbh|b\.?v\.?|ag|s\.?a\.?|s\.?r\.?l\.?|pte\.?\s*ltd\.?|pty\.?\s*ltd\.?|plc)\s*$",
    re.I,
)


def short(organisation: str) -> str:
    """A maker's short name from the registry's full name and address."""
    name = organisation.split("\n", 1)[0].strip()
    lowered = name.lower()
    for key, known in _KNOWN.items():
        if re.search(rf"\b{re.escape(key)}\b", lowered):  # whole words: "Engineering" is not Ring
            return known
    while (trimmed := _SUFFIX.sub("", name).strip(" ,.")) != name:
        name = trimmed
    return name.title() if name.isupper() and len(name) > 4 else name


def main(source: Path) -> None:
    registry = json.loads(source.read_text(encoding="utf-8"))
    makers = {prefix.upper(): short(organisation) for prefix, organisation in registry.items() if len(prefix) == 6}
    OUT.write_bytes(gzip.compress(json.dumps(makers, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(f"{len(makers)} prefixes, {len(set(makers.values()))} makers -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
