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

wheels_subdirs = os.listdir(WHEELS_DIR)
new_wheels = 0
existing_count = 0

for subdir in wheels_subdirs:
    wheel_files = os.listdir(f"{WHEELS_DIR}{os.sep}{subdir}")

    for wheel in wheel_files:
        pattern = re.compile(r"^(.+?)-(\d+)")
        match = pattern.search(wheel)
        if match:
            wheel_name = match.group(1)
            wheel_name = normalize(wheel_name)

            is_new = f"pypi/{wheel_name}/{wheel}" not in existing_wheels

            BUCKET.upload_file(f"{WHEELS_DIR}{os.sep}{subdir}{os.sep}{wheel}", f"pypi/{wheel_name}/{wheel}")

            if is_new:
                new_wheels += 1
                print_color(f"++ {wheel_name}/{wheel}", Fore.GREEN)
            else:
                existing_count += 1
                print(f"   {wheel_name}/{wheel}")

print_color("---------- END UPLOADING ----------")

print_color("---------- STATISTICS ----------")
print_color(f"New wheels: {new_wheels}", Fore.GREEN)
print(f"Existing wheels (re-uploaded): {existing_count}")
print(f"Total uploaded: {new_wheels + existing_count}")
print_color("---------- END STATISTICS ----------")
