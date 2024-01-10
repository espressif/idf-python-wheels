#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
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


s3 = boto3.resource('s3')
try:
    BUCKET = s3.Bucket(sys.argv[1])
except IndexError:
    raise SystemExit('Error: S3 bucket name not provided.')

WHEELS_DIR = f'{os.path.curdir}{(os.sep)}downloaded_wheels'
if not os.path.exists(WHEELS_DIR):
    raise SystemExit(f'Error: The wheels directory {WHEELS_DIR} not found.')

wheels_subdirs = os.listdir(WHEELS_DIR)

for subdir in wheels_subdirs:
    wheel_files = os.listdir(f'{WHEELS_DIR}{os.sep}{subdir}')

    for wheel in wheel_files:
        pattern = re.compile(r'(\w*)-(\d+)')
        match = pattern.search(wheel)
        if match:
            wheel_name = match.group(1)

        wheel_name = wheel_name.lower()
        wheel_name = wheel_name.replace('_', '-')

        BUCKET.upload_file(f'{WHEELS_DIR}{os.sep}{subdir}{os.sep}{wheel}', f'pypi/{wheel_name}/{wheel}')
        print(f'Uploaded {wheel}')
