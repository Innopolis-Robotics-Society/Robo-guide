#!/usr/bin/env python3
"""Extract one complete serialized ROS message, without deserialization or ROS dependencies."""
import argparse
import base64
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("source")
    parser.add_argument("sequence", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.journal.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("source") != args.source or row.get("sequence") != args.sequence:
                continue
            if row.get("encoding") != "cdr-base64":
                parser.error("selected record has no complete CDR payload")
            payload = base64.b64decode(row["data"], validate=True)
            if len(payload) != row["byte_length"]:
                parser.error("payload length mismatch")
            with args.output.open("xb") as output:
                output.write(payload)
            print(row["ros_type"], len(payload), "bytes")
            return
    parser.error("record not found")


if __name__ == "__main__":
    main()
