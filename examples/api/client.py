#!/usr/bin/env python3
"""Standalone stdlib client: login, state, tour, result and stop; no UI imports."""

import argparse
import getpass
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
import uuid


class Robot:
    """Same session API used by a browser, with a private in-memory cookie jar."""

    def __init__(self, base):
        """Create a session client without persisting credentials on disk."""
        self.base = base.rstrip("/") + "/api/v1"
        self.http = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method, path, body=None, key=None):
        """Send once. Caller retains the key if a network failure makes outcome unknown."""
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with self.http.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode()) from exc

    def login(self, pin):
        """Use a one-time challenge; PIN and session never go in the URL."""
        challenge = self.request("POST", "/auth/challenge", {})
        return self.request(
            "POST", "/auth/verify", {"nonce": challenge["nonce"], "backend": "pin", "pin": pin}
        )


def main():
    """Run a selected tour, observing the operation; Ctrl-C requests stop in place."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8089")
    parser.add_argument(
        "--pose",
        nargs=3,
        type=float,
        metavar=("X", "Y", "YAW"),
        help="Drive to a pose on the current map",
    )
    parser.add_argument("--map", action="store_true", help="Inspect map and localization presets")
    parser.add_argument(
        "--volume", type=float, help="Set system speaker volume in allowed range 0..100"
    )
    parser.add_argument("--wifi-scan", action="store_true", help="List available Wi-Fi networks")
    parser.add_argument("--tour", help="Omit to only inspect state and tours")
    parser.add_argument("--stop-after", type=float, help="Request mission stop after N seconds")
    args = parser.parse_args()
    robot = Robot(args.url)
    robot.login(getpass.getpass("Operator PIN: "))
    try:
        state = robot.request("GET", "/state")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        localization = (state.get("robot") or {}).get("localization", {})
        if localization.get("motion_blocked"):
            print("Движение заблокировано:", localization.get("reason"),
                  "Задайте положение через /localization/reset и дождитесь ready.")
        print(json.dumps(robot.request("GET", "/tours"), ensure_ascii=False, indent=2))
        if args.map:
            print(robot.request("GET", "/map"))
            print(robot.request("GET", "/localization/presets"))
        if args.volume is not None:
            print(robot.request("POST", "/audio/output", {"volume": args.volume}))
        if args.wifi_scan:
            print(robot.request("POST", "/wifi/scan", {}))
        if not args.tour and not args.pose:
            return
        if args.tour and args.pose:
            raise ValueError("Choose --tour or --pose")
        key = str(uuid.uuid4())
        print("Idempotency-Key:", key, flush=True)
        if args.pose:
            current_map = robot.request("GET", "/map")
            operation = robot.request(
                "POST",
                "/missions/go-to-pose",
                {
                    "map_version": current_map["version"],
                    "pose": dict(zip(("x", "y", "yaw"), args.pose, strict=True)),
                },
                key,
            )
        else:
            operation = robot.request("POST", "/missions", {"tour_id": args.tour}, key)
        print("Operation:", operation["id"], flush=True)
        began = time.monotonic()
        try:
            while operation["status"] in ("pending", "running"):
                time.sleep(0.5)
                if args.stop_after is not None and time.monotonic() - began >= args.stop_after:
                    print(robot.request("POST", "/missions/stop", {}))
                    args.stop_after = None
                operation = robot.request("GET", "/operations/" + operation["id"])
                print(json.dumps(operation, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            print(robot.request("POST", "/missions/stop", {}))
        if operation["status"] == "unknown":
            print("Outcome unknown. Inspect current robot state; do not automatically resend.")
    finally:
        robot.request("POST", "/auth/logout", {})


if __name__ == "__main__":
    main()
