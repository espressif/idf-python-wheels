#
# SPDX-FileCopyrightText: 2023-2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""This script uploads wheel files from the downloaded wheels directory to S3 bucket.
- argument S3 bucket
"""

import hashlib
import os
import re
import sys

from typing import Optional

import boto3

from botocore.exceptions import ClientError
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


def _file_md5_hex(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _overwrite_would_hide_different_wheel(s3_key: str, local_path: str) -> Optional[str]:
    """Return an error message if an existing object differs from local_path; else None."""
    obj = BUCKET.Object(s3_key)
    try:
        obj.load()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey"):
            return None
        raise
    remote_size = obj.content_length
    local_size = os.path.getsize(local_path)
    if remote_size != local_size:
        return f"Refusing to overwrite {s3_key}: remote size {remote_size} != local size {local_size}"
    etag = (obj.e_tag or "").strip('"')
    if not etag or "-" in etag:
        # Multipart upload ETag is not a raw MD5; size match is the best check here.
        return None
    local_md5 = _file_md5_hex(local_path)
    if etag != local_md5:
        return (
            f"Refusing to overwrite {s3_key}: remote ETag {etag!r} != local MD5 {local_md5!r}. "
            "Same wheel filename would publish different bytes (e.g. ARMv7 vs ARMv7 Legacy collision)."
        )
    return None


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

        s3_key = f"pypi/{wheel_name}/{wheel}"
        is_new = s3_key not in existing_wheels

        conflict = _overwrite_would_hide_different_wheel(s3_key, full_path)
        if conflict:
            raise SystemExit(conflict)

        BUCKET.upload_file(full_path, s3_key, ExtraArgs={"ACL": "public-read"})

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
