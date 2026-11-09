"""Protocol diagnostics using the same production SerialLink."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace

from .config import load_config
from .serial_link import SerialLink, TransportError
from titella_slave_assets.wire import FIRMWARE_TO_MASTER, FrameParser, base_frame, encode_frame


def simulate() -> dict[str, object]:
    link_id = "0123456789abcdef0123456789abcdef"
    frame = base_frame(link_id, "hid", "response", request_id=1, op="health", body={"ok": True, "status": "hid_ready"})
    payload = encode_frame(frame)
    parser = FrameParser(FIRMWARE_TO_MASTER)
    parsed = []
    for byte in payload:
        parsed.extend(parser.feed(bytes((byte,))))
    return {"ok": parsed == [frame], "frames": len(parsed), "max_frame_bytes": 4096, "chunk_bytes": 512}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Probes titella-esp32 with the production link")
    parser.add_argument(
        "port",
        nargs="?",
        help="UART (/dev/...) or Wi-Fi host[:port]; overrides the configuration",
    )
    parser.add_argument("--config", help="path to the TOML")
    parser.add_argument("--simulate", action="store_true", help="validates framing without hardware")
    args = parser.parse_args(argv)
    if args.simulate:
        result = simulate()
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)

    config = load_config(args.config)
    if args.port:
        if config.transport == "wifi":
            host, separator, port = args.port.rpartition(":")
            if separator and port.isdigit():
                config = replace(config, wifi_host=host, wifi_port=int(port))
            else:
                config = replace(config, wifi_host=args.port)
        else:
            config = replace(config, serial_port=args.port)
    link = SerialLink(config)
    try:
        if not link.wait_connected(6):
            raise TransportError(
                "connect_timeout", "the link could not be opened", True
            )
        response = link.request("hid", "health", {}, timeout=6)
        result = {"ok": True, "port": link.port, **dict(response.get("body") or {})}
        print(json.dumps(result, ensure_ascii=False))
    except TransportError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False))
        raise SystemExit(1)
    finally:
        link.close()


if __name__ == "__main__":
    main()
