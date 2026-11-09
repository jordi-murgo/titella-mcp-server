"""TOML and environment configuration for the HID auditor."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("~/.config/titella/config.toml").expanduser()
DEFAULT_TITELLA_ID = "default"
_TITELLA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

ENV_TO_KEY = {
    "TITELLA_TRANSPORT": "transport",
    "TITELLA_SERIAL_PORT": "serial_port",
    "TITELLA_SERIAL_BAUD": "serial_baud",
    "TITELLA_WIFI_HOST": "wifi_host",
    "TITELLA_WIFI_PORT": "wifi_port",
    "TITELLA_SCREEN_DEVICE": "screen_device",
    "TITELLA_SCREEN_INPUT_FORMAT": "screen_input_format",
    "TITELLA_SCREEN_PIXEL_FORMAT": "screen_pixel_format",
    "TITELLA_SCREEN_FRAMERATE": "screen_framerate",
    "TITELLA_STATE_DIR": "state_dir",
    "TITELLA_KEYBOARD_LAYOUT": "keyboard_layout",
    "TITELLA_HEARTBEAT_S": "heartbeat_s",
    "TITELLA_DISCONNECT_GRACE_S": "disconnect_grace_s",
    "TITELLA_HTTP_PORT": "http_port",
    "TITELLA_SLAVE_PLATFORM": "slave_platform",
    "TITELLA_SLAVE_CDC_PORT": "slave_cdc_port",
    "TITELLA_TERMINAL_MODS": "terminal_mods",
    "TITELLA_TERMINAL_KEY": "terminal_key",
    "TITELLA_LAUNCHER_TEXT": "launcher_text",
    "TITELLA_DEFAULT_TITELLA": "default_titella",
}

_INSTANCE_KEYS = frozenset({
    "transport",
    "serial_port",
    "serial_baud",
    "wifi_host",
    "wifi_port",
    "screen_device",
    "screen_input_format",
    "screen_pixel_format",
    "screen_framerate",
    "keyboard_layout",
    "slave_platform",
    "slave_cdc_port",
    "terminal_mods",
    "terminal_key",
    "launcher_text",
    "heartbeat_s",
    "disconnect_grace_s",
    "ack_timeout_s",
    "ack_retries",
})


def validate_titella_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("titella id must be a string")
    cleaned = value.strip()
    if not _TITELLA_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"invalid titella id {value!r}: must match [a-z0-9][a-z0-9_-]{{0,62}}"
        )
    return cleaned


@dataclass(frozen=True)
class Config:
    # Master→ESP32 transport: UART ("serial") or TCP over the SoftAP ("wifi").
    # Port autodetection and the DTR/RTS lines only apply to serial.
    transport: str = "serial"
    serial_port: str | None = None
    wifi_host: str = "192.168.4.1"
    wifi_port: int = 5555
    # HDMI→USB capture device connected to the master; capture is local.
    screen_device: str = "USB3 Video"
    screen_input_format: str = ""      # empty = avfoundation on macOS, v4l2 on Linux
    screen_pixel_format: str = "uyvy422"
    screen_framerate: int = 30
    state_dir: Path = Path(tempfile.gettempdir()) / "titella"
    keyboard_layout: str = "es_ES"
    heartbeat_s: float = 5.0
    disconnect_grace_s: float = 30.0
    http_port: int = 8765
    # Platform of the slave the bootstrap will type into. It is never inferred
    # from the master: an explicit selection avoids launching a payload that
    # cannot run on the target.
    slave_platform: str | None = None
    # Native CDC device of the slave; when unset the loader probes its own OS.
    slave_cdc_port: str | None = None
    # Optional launcher overrides for the configured default slave platform,
    # or for Linux where no portable terminal shortcut exists. An explicitly
    # selected macOS or Windows target uses its built-in profile so a shortcut
    # for another OS cannot leak across targets.
    terminal_mods: tuple[str, ...] | None = None
    terminal_key: str | None = None
    launcher_text: str | None = None
    serial_baud: int = 115200
    ack_timeout_s: float = 1.0
    ack_retries: int = 3
    titella_id: str = DEFAULT_TITELLA_ID
    default_titella: str | None = None
    titellas: Mapping[str, "Config"] = field(default_factory=dict, repr=False, compare=False)
    selected_config_path: Path = field(default=DEFAULT_CONFIG_PATH, repr=False, compare=False)

    @property
    def config_path(self) -> Path:
        return self.selected_config_path

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        return self.state_dir


def default_input_format() -> str:
    """ffmpeg capture backend for the master's operating system."""
    return "avfoundation" if sys.platform == "darwin" else "v4l2"


def _read_raw_toml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid TOML configuration: {path}")
    return value


def _split_document(document: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    """Extracts root defaults and named ``[titella.<id>]`` tables.

    Supports both:
    - flat root keys plus optional ``[titella.<id>]`` subtables; and
    - legacy single-table ``[titella]`` with scalar settings.
    """
    root_values: dict[str, Any] = {
        key: value for key, value in document.items() if key != "titella"
    }
    named_tables: dict[str, Mapping[str, Any]] = {}
    titella_section = document.get("titella")
    if isinstance(titella_section, Mapping):
        for key, value in titella_section.items():
            if isinstance(value, Mapping):
                tid = validate_titella_id(key)
                named_tables[tid] = dict(value)
            elif key not in root_values:
                root_values[key] = value
    return root_values, named_tables


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _build_single_config(
    values: Mapping[str, Any],
    *,
    titella_id: str,
    state_dir: Path,
    http_port: int,
    default_titella: str | None,
    selected_path: Path,
) -> Config:
    layout = str(values.get("keyboard_layout", "es_ES"))
    if layout not in {"es_ES", "en_US"}:
        raise ValueError("keyboard_layout must be es_ES or en_US")
    transport = str(values.get("transport", "serial")).strip().lower()
    if transport not in {"serial", "wifi"}:
        raise ValueError("transport must be serial or wifi")
    # The TCP port is always validated: an impossible value is a configuration
    # error even when the active transport is serial.
    wifi_port = _integer(values.get("wifi_port", 5555), "wifi_port", 1, 65535)
    wifi_host = str(values.get("wifi_host", "192.168.4.1")).strip()
    if transport == "wifi" and not wifi_host:
        raise ValueError("wifi_host cannot be empty with transport=wifi")
    heartbeat = _number(values.get("heartbeat_s", 5), "heartbeat_s", 1, 60)
    grace = _number(values.get("disconnect_grace_s", 30), "disconnect_grace_s", heartbeat, 600)
    ack_timeout = _number(values.get("ack_timeout_s", 1), "ack_timeout_s", 0.1, 10)
    ack_retries = int(values.get("ack_retries", 3))
    if not 0 <= ack_retries <= 10:
        raise ValueError("ack_retries must be between 0 and 10")

    serial_port = values.get("serial_port")
    if serial_port is not None:
        serial_port = str(serial_port).strip() or None
    serial_baud = _integer(values.get("serial_baud", 115200), "serial_baud", 1, 5_000_000)

    # The slave platform is a hard selection, never inferred from the master.
    # An invalid value is a configuration error even before any bootstrap runs.
    slave_platform = values.get("slave_platform")
    if slave_platform is not None:
        slave_platform = str(slave_platform).strip().lower()
        if not slave_platform:
            slave_platform = None
        elif slave_platform not in {"macos", "linux", "windows"}:
            raise ValueError("slave_platform must be macos, linux or windows")
    slave_cdc_port = values.get("slave_cdc_port")
    if slave_cdc_port is not None:
        slave_cdc_port = str(slave_cdc_port).strip() or None
    if (
        slave_platform == "windows"
        and slave_cdc_port is not None
        and re.fullmatch(r"COM[1-9][0-9]*", slave_cdc_port) is None
    ):
        raise ValueError("slave_cdc_port must match COM[1-9][0-9]* on windows")

    # Launcher fields stay optional: None defers to the platform profile, an
    # explicit empty string (launcher_text="") is authoritative.
    mods = values.get("terminal_mods")
    if mods is None:
        terminal_mods: tuple[str, ...] | None = None
    elif isinstance(mods, str):
        terminal_mods = tuple(part.strip().upper() for part in mods.split(",") if part.strip())
    else:
        terminal_mods = tuple(str(part).strip().upper() for part in mods)
    terminal_key = values.get("terminal_key")
    if terminal_key is not None:
        terminal_key = str(terminal_key).strip().upper() or None
    launcher_text = values.get("launcher_text")
    if launcher_text is not None:
        launcher_text = str(launcher_text)
    screen_device = str(values.get("screen_device", "USB3 Video")).strip()
    if not screen_device:
        raise ValueError("screen_device cannot be empty")
    screen_input_format = str(values.get("screen_input_format", "")).strip() or default_input_format()
    screen_pixel_format = str(values.get("screen_pixel_format", "uyvy422")).strip()
    if not screen_pixel_format:
        raise ValueError("screen_pixel_format cannot be empty")
    screen_framerate = _integer(values.get("screen_framerate", 30), "screen_framerate", 1, 240)
    return Config(
        transport=transport,
        serial_port=serial_port,
        wifi_host=wifi_host,
        wifi_port=wifi_port,
        screen_device=screen_device,
        screen_input_format=screen_input_format,
        screen_pixel_format=screen_pixel_format,
        screen_framerate=screen_framerate,
        state_dir=state_dir,
        keyboard_layout=layout,
        heartbeat_s=heartbeat,
        disconnect_grace_s=grace,
        http_port=http_port,
        slave_platform=slave_platform,
        slave_cdc_port=slave_cdc_port,
        terminal_mods=terminal_mods,
        terminal_key=terminal_key,
        launcher_text=launcher_text,
        serial_baud=serial_baud,
        ack_timeout_s=ack_timeout,
        ack_retries=ack_retries,
        titella_id=titella_id,
        default_titella=default_titella,
        selected_config_path=selected_path,
    )


def instance_state_dir(base_state_dir: Path, titella_id: str) -> Path:
    """State directory for one titella instance.

    The legacy ``default`` instance keeps using ``base_state_dir`` directly so
    existing ``active-link-id`` and lease files remain compatible across upgrades.
    Named instances use ``base_state_dir / "titellas" / <id>``.
    """
    if titella_id == DEFAULT_TITELLA_ID:
        return base_state_dir
    return base_state_dir / "titellas" / titella_id


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Loads defaults < TOML < environment; ``--config`` selects the TOML."""
    env = os.environ if environ is None else environ
    selected = Path(
        config_path
        or env.get("TITELLA_CONFIG", "")
        or DEFAULT_CONFIG_PATH
    ).expanduser()
    raw_doc = _read_raw_toml(selected)
    values, named_tables = _split_document(raw_doc)
    for env_name, key in ENV_TO_KEY.items():
        if env_name in env:
            values[key] = env[env_name]

    base_state_dir = Path(
        values.get("state_dir", Path(tempfile.gettempdir()) / "titella")
    ).expanduser()
    http_port = _integer(values.get("http_port", 8765), "http_port", 1024, 65535)

    default_titella_raw = values.get("default_titella")
    default_titella: str | None = None
    if default_titella_raw is not None and str(default_titella_raw).strip():
        default_titella = validate_titella_id(str(default_titella_raw))

    if not named_tables:
        chosen_id = default_titella or DEFAULT_TITELLA_ID
        single = _build_single_config(
            values,
            titella_id=chosen_id,
            state_dir=instance_state_dir(base_state_dir, chosen_id) if chosen_id != DEFAULT_TITELLA_ID else base_state_dir,
            http_port=http_port,
            default_titella=chosen_id,
            selected_path=selected,
        )
        return replace(single, titellas={chosen_id: single})

    if default_titella is not None and default_titella not in named_tables:
        raise ValueError(
            f"default_titella {default_titella!r} is not defined in [titella.<id>]"
        )
    if default_titella is None and len(named_tables) == 1:
        default_titella = next(iter(named_tables))

    built: dict[str, Config] = {}
    for tid, table in named_tables.items():
        merged = {k: v for k, v in values.items() if k in _INSTANCE_KEYS}
        merged.update(table)
        # Environment variables explicitly set still win for the default/single instance.
        for env_name, key in ENV_TO_KEY.items():
            if env_name in env and key in _INSTANCE_KEYS:
                if default_titella is None or tid == default_titella:
                    merged[key] = env[env_name]
        # If the user migrated from a flat config to a single named [titella.<id>]
        # and active-link-id already exists at base_state_dir root, seed the new
        # instance state_dir on first ensure so live agents do not lose their link_id.
        sub_state = instance_state_dir(base_state_dir, tid)
        built[tid] = _build_single_config(
            merged,
            titella_id=tid,
            state_dir=sub_state,
            http_port=http_port,
            default_titella=default_titella,
            selected_path=selected,
        )

    primary_id = default_titella or next(iter(built))
    primary = built[primary_id]
    return replace(
        primary,
        state_dir=base_state_dir,
        titella_id=primary_id,
        default_titella=default_titella,
        titellas=built,
        selected_config_path=selected,
    )


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_format_toml_value(str(item)) for item in value)
        return f"[{inner}]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_titella_config(
    path: Path,
    *,
    global_values: Mapping[str, Any],
    titellas: Mapping[str, Mapping[str, Any]],
    default_titella: str | None,
) -> None:
    """Atomically writes ``config.toml`` with global keys and ``[titella.<id>]`` tables."""
    lines: list[str] = []
    ordered_globals = (
        "state_dir",
        "http_port",
        "heartbeat_s",
        "disconnect_grace_s",
        "default_titella",
        "transport",
        "serial_port",
        "serial_baud",
        "wifi_host",
        "wifi_port",
        "screen_device",
        "screen_input_format",
        "screen_pixel_format",
        "screen_framerate",
        "keyboard_layout",
        "slave_platform",
        "slave_cdc_port",
        "terminal_mods",
        "terminal_key",
        "launcher_text",
    )
    merged_globals = dict(global_values)
    if default_titella is not None:
        merged_globals["default_titella"] = default_titella
    else:
        merged_globals.pop("default_titella", None)

    for key in ordered_globals:
        if key in merged_globals and merged_globals[key] is not None:
            lines.append(f"{key} = {_format_toml_value(merged_globals[key])}")
    for key in sorted(merged_globals):
        if key not in ordered_globals and key != "titella" and merged_globals[key] is not None:
            lines.append(f"{key} = {_format_toml_value(merged_globals[key])}")

    ordered_instance_keys = (
        "transport",
        "serial_port",
        "serial_baud",
        "wifi_host",
        "wifi_port",
        "screen_device",
        "screen_input_format",
        "screen_pixel_format",
        "screen_framerate",
        "keyboard_layout",
        "slave_platform",
        "slave_cdc_port",
        "terminal_mods",
        "terminal_key",
        "launcher_text",
        "heartbeat_s",
        "disconnect_grace_s",
    )
    for tid in sorted(titellas):
        validate_titella_id(tid)
        table = titellas[tid]
        if lines:
            lines.append("")
        lines.append(f"[titella.{tid}]")
        for key in ordered_instance_keys:
            if key in table and table[key] is not None:
                lines.append(f"{key} = {_format_toml_value(table[key])}")
        for key in sorted(table):
            if key not in ordered_instance_keys and table[key] is not None:
                lines.append(f"{key} = {_format_toml_value(table[key])}")

    content = "\n".join(lines) + "\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise
