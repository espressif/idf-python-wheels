#
# SPDX-FileCopyrightText: 2023-2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""This script uploads wheel files from the downloaded wheels directory to S3 bucket.
- argument S3 bucket
"""

import os
import re
import sys

import boto3

from colorama import Fore

from _helper_functions import print_color

s3 = boto3.resource("s3")
try:
    BUCKET = s3.Bucket(sys.argv[1])
except IndexError:
    raise SystemExit("Error: S3 bucket name not provided.")

WHEELS_DIR = f"{os.path.curdir}{(os.sep)}downloaded_wheels"
if not os.path.exists(WHEELS_DIR):
    raise SystemExit(f"Error: The wheels directory {WHEELS_DIR} not found.")


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def get_existing_wheels():
    """Get set of S3 keys for wheels currently on server."""
    existing = set()
    for obj in BUCKET.objects.filter(Prefix="pypi/"):
        if obj.key.endswith(".whl"):
            existing.add(obj.key)
    return existing


print_color("---------- UPLOAD WHEELS TO S3 ----------")

existing_wheels = get_existing_wheels()
print(f"Found {len(existing_wheels)} existing wheels on S3\n")

print_color("---------- UPLOADING WHEELS ----------")


def collect_wheel_paths():
    """Collect (full_path, wheel_filename) for all .whl files in WHEELS_DIR.
    Handles both flat layout (wheels directly in dir) and nested (wheels in subdirs).
    """
    collected = []
    for item in os.listdir(WHEELS_DIR):
        path = os.path.join(WHEELS_DIR, item)
        if os.path.isfile(path) and item.endswith(".whl"):
            collected.append((path, item))
        elif os.path.isdir(path):
            for wheel in os.listdir(path):
                if wheel.endswith(".whl"):
                    collected.append((os.path.join(path, wheel), wheel))
    return collected


wheel_paths = collect_wheel_paths()
new_wheels = 0
existing_count = 0

for full_path, wheel in wheel_paths:
    pattern = re.compile(r"^(.+?)-(\d+)")
    match = pattern.search(wheel)
    if match:
        wheel_name = match.group(1)
        wheel_name = normalize(wheel_name)

        is_new = f"pypi/{wheel_name}/{wheel}" not in existing_wheels

        BUCKET.upload_file(full_path, f"pypi/{wheel_name}/{wheel}")

        if is_new:
            new_wheels += 1
            print_color(f"++ {wheel_name}/{wheel}", Fore.GREEN)
        else:
            existing_count += 1
            print(f"  <- {wheel_name}/{wheel}")

print_color("---------- END UPLOADING ----------")

print_color("---------- STATISTICS ----------")
print_color(f"New wheels: {new_wheels}", Fore.GREEN)
print(f"Existing wheels (re-uploaded): {existing_count}")
print(f"Total uploaded: {new_wheels + existing_count}")
print_color("---------- END STATISTICS ----------")
