"""Screen brightness on macOS: the built-in screen through osascript, external monitors through m1ddc.

The built-in screen: DisplayServices is the private framework behind the
brightness slider in System Settings. JavaScript for Automation can load it
and bind its C functions, so this needs nothing installed. Being private, a
macOS update can change it; the error then says what failed.

External monitors do not take brightness from macOS: a Dell or an LG is set
over its cable with DDC/CI. m1ddc (MIT, `brew install m1ddc`) does that on
Apple Silicon. Without it, only the built-in screen is listed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys

from tools.specialized._display import Screen

logger = logging.getLogger(__name__)

# Finds the built-in screen among all of them, so it is found even when the
# menu bar, and so the "main" display, is on a monitor.
_BUILT_IN_SCRIPT = """
function run(argv) {
  ObjC.import("AppKit");
  $.NSBundle.bundleWithPath("/System/Library/PrivateFrameworks/DisplayServices.framework").load;
  ObjC.bindFunction("CGDisplayIsBuiltin", ["bool", ["unsigned int"]]);
  ObjC.bindFunction("DisplayServicesCanChangeBrightness", ["bool", ["unsigned int"]]);
  ObjC.bindFunction("DisplayServicesGetBrightness", ["int", ["unsigned int", "float *"]]);
  ObjC.bindFunction("DisplayServicesSetBrightness", ["int", ["unsigned int", "float"]]);
  var screens = $.NSScreen.screens, screen = null, display = 0;
  for (var i = 0; i < screens.count && screen === null; i++) {
    var id = screens.objectAtIndex(i).deviceDescription.objectForKey("NSScreenNumber").unsignedIntValue;
    if ($.CGDisplayIsBuiltin(id)) { screen = screens.objectAtIndex(i); display = id; }
  }
  if (screen === null) throw new Error("the built-in screen is off");
  if (!$.DisplayServicesCanChangeBrightness(display))
    throw new Error("the built-in screen's brightness cannot be set");
  if (argv.length && $.DisplayServicesSetBrightness(display, parseFloat(argv[0])) !== 0)
    throw new Error("setting brightness failed");
  var level = Ref();
  if ($.DisplayServicesGetBrightness(display, level) !== 0) throw new Error("reading brightness failed");
  return screen.localizedName.js + "\\t" + level[0];
}
"""

# "[2] AW3425DWM (1C056EE6-3EC6-4232-8AA5-D2794825F034)"
_MONITOR_LINE = re.compile(r"^\[\d+\]\s+(?P<name>.+?)\s+\((?P<uuid>[0-9A-Fa-f-]{36})\)$")

# A monitor DDC/CI does not reach still answers m1ddc, with noise: a Dell
# P2419H behind an adapter read 16% brightness, a maximum of -126, and volume
# 98 on a monitor with no speakers.
_NO_DDC = (
    "the monitor is not answering brightness commands. Turn on DDC/CI in its own menu, "
    "or connect it with a cable that carries DDC/CI (many USB-C to HDMI adapters do not)"
)

_TIMEOUT_SECONDS = 10.0


class MacDisplay:
    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    @staticmethod
    async def screens() -> list[Screen]:
        found: list[Screen] = []
        try:
            name, _ = await _built_in()
            found.append(Screen(name, built_in=True))
        except OSError:
            logger.debug("no built-in screen to control", exc_info=True)  # lid closed, or a Mac mini
        found.extend(Screen(name, built_in=False) for name in await _monitors())
        return found

    @staticmethod
    async def brightness(screen: Screen, level: int | None = None) -> int:
        if screen.built_in:
            _, reading = await _built_in(level)
            return reading
        uuid = (await _monitors()).get(screen.name)
        if uuid is None:
            raise OSError(f"{screen.name} is no longer connected")
        return await _monitor_brightness(uuid, level)


async def _built_in(level: int | None = None) -> tuple[str, int]:
    argv = ["osascript", "-l", "JavaScript", "-e", _BUILT_IN_SCRIPT]
    if level is not None:
        argv.append(f"{level / 100:.4f}")
    answer = await _run(argv)
    name, _, reading = answer.rpartition("\t")
    try:
        return name, round(float(reading) * 100)
    except ValueError:
        raise OSError(f"osascript returned {answer!r}, not a brightness") from None


async def _monitors() -> dict[str, str]:
    """External monitors m1ddc can address, {name: uuid}. Empty without m1ddc."""
    if shutil.which("m1ddc") is None:
        return {}
    try:
        listing = await _run(["m1ddc", "display", "list"])
    except OSError:
        logger.warning("m1ddc could not list the monitors", exc_info=True)
        return {}
    monitors: dict[str, str] = {}
    for line in listing.splitlines():
        match = _MONITOR_LINE.match(line.strip())
        if match is None or match["name"] == "(null)":  # (null) is the built-in screen, which has no DDC/CI
            continue
        name, copy = match["name"], 2
        while name in monitors:  # two of the same monitor
            name, copy = f"{match['name']} ({copy})", copy + 1
        monitors[name] = match["uuid"]
    return monitors


async def _monitor_brightness(uuid: str, level: int | None) -> int:
    maximum = _whole(await _run(["m1ddc", "display", uuid, "max", "luminance"]))
    if maximum is None or not 0 < maximum <= 1000:
        raise OSError(_NO_DDC)
    wanted = None if level is None else round(level * maximum / 100)
    if wanted is not None:
        await _run(["m1ddc", "display", uuid, "set", "luminance", str(wanted)])
    raw = _whole(await _run(["m1ddc", "display", uuid, "get", "luminance"]))
    # Exact: the Dell asked for 17 read back 16, one step from what it was asked.
    if raw is None or not 0 <= raw <= maximum or (wanted is not None and raw != wanted):
        raise OSError(_NO_DDC)
    return round(raw * 100 / maximum)


def _whole(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


async def _run(argv: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), _TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise OSError(f"{argv[0]} did not answer in {_TIMEOUT_SECONDS:.0f}s") from None
    if process.returncode != 0:
        raise OSError(_reason(stderr.decode(errors="replace") or stdout.decode(errors="replace")))
    return stdout.decode(errors="replace").strip()


def _reason(output: str) -> str:
    """osascript's "execution error: Error: Error: <reason> (-2700)" as just the reason."""
    reason = re.sub(r"^.*?execution error: (?:Error: )*", "", output.strip())
    return re.sub(r"\s*\(-?\d+\)$", "", reason) or "the command failed"
