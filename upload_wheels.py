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

wheel_files = os.listdir(WHEELS_DIR)

for wheel in wheel_files:
    pattern = re.compile(r'([^ -]*)-(\d+)')
    match = pattern.search(wheel)
    if match:
        wheel_name = match.group(1)

    wheel_name = wheel_name.lower()
    wheel_name = wheel_name.replace('_', '-')

    if sys.platform in 'CYGWIN,MINGW,MINGW32,MSYS' and wheel_name == 'esptool':
        continue

    BUCKET.upload_file(f'{WHEELS_DIR}{os.sep}{wheel}', f'pypi/{wheel_name}/{wheel}')
    print(f'Uploaded {wheel}')
