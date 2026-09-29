"""Small custom-command interface for smc-bridge."""
import argparse
import json
import os
import sys

from .client import HRDClient, HRDError, OutcomeUnknown


def main(argv=None):
    parser = argparse.ArgumentParser(description="Control HRD through its native TCP IP Server")
    parser.add_argument("--host", default=os.environ.get("HRD_HOST", "172.16.10.3"))
    parser.add_argument("--port", type=int, default=os.environ.get("HRD_PORT", "7809"))
    parser.add_argument("--timeout", type=float, default=os.environ.get("HRD_TIMEOUT", "5"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("frequency", "radio", "sliders", "buttons", "dropdowns"):
        commands.add_parser(name)
    query = commands.add_parser("get", help="Read-only: send an HRD get command and print the raw reply")
    query.add_argument("text", nargs="+", help="The rest of the command, such as: button-select TX")
    tune = commands.add_parser("tune", help="Adjust frequency by signed Hz")
    tune.add_argument("delta", type=int)
    slider = commands.add_parser("slider", help="Adjust a slider by signed raw units")
    slider.add_argument("name")
    slider.add_argument("delta", type=int)
    info = commands.add_parser("slider-info", help="Read slider limits and position")
    info.add_argument("name")
    button = commands.add_parser("button", help="Set a button on (press) or off (release)")
    button.add_argument("name")
    button.add_argument("state", choices=("on", "off"))
    unkey = commands.add_parser("unkey", help="Release PTT: set the TX button (or --button) off")
    unkey.add_argument("--button", default="TX")
    dropdown = commands.add_parser("dropdown", help="Show a dropdown's value and choices, or select a value")
    dropdown.add_argument("name")
    dropdown.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    try:
        with HRDClient(args.host, args.port, args.timeout) as client:
            if args.command == "tune":
                result = client.tune(args.delta)
            elif args.command == "slider":
                result = client.adjust_slider(args.name, args.delta)
            elif args.command == "slider-info":
                result = client.get_slider(args.name)
            elif args.command == "get":
                result = client.query(" ".join(args.text))
            elif args.command == "button":
                client.press_button(args.name, args.state == "on")
                result = args.state
            elif args.command == "unkey":
                # No list check: a panic stop sends the release straight away.
                client.press_button(args.button, False, check=False)
                result = "off"
            elif args.command == "dropdown":
                if args.value is None:
                    result = client.get_dropdown(args.name)
                else:
                    result = client.set_dropdown(args.name, args.value)
            else:
                result = getattr(client, "get_" + args.command)()
            print(json.dumps(result) if isinstance(result, (dict, list)) else result)
        return 0
    except OutcomeUnknown as exc:
        print(f"hrdctl: {exc}", file=sys.stderr)
        return 3
    except (HRDError, ValueError) as exc:
        print(f"hrdctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
