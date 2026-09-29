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
    for name in ("frequency", "radio", "sliders"):
        commands.add_parser(name)
    tune = commands.add_parser("tune", help="Adjust frequency by signed Hz")
    tune.add_argument("delta", type=int)
    slider = commands.add_parser("slider", help="Adjust a slider by signed raw units")
    slider.add_argument("name")
    slider.add_argument("delta", type=int)
    info = commands.add_parser("slider-info", help="Read slider limits and position")
    info.add_argument("name")
    args = parser.parse_args(argv)
    try:
        with HRDClient(args.host, args.port, args.timeout) as client:
            if args.command == "tune":
                result = client.tune(args.delta)
            elif args.command == "slider":
                result = client.adjust_slider(args.name, args.delta)
            elif args.command == "slider-info":
                result = client.get_slider(args.name)
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
