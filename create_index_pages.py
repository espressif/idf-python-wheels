#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Dict

import boto3

def _html_loader(path: str) -> str:
    """Loads the HTML file"""
    with open(path, 'r') as file:
        return file.read()

HTML_HEADER = _html_loader('resources/html/header.html')
HTML_PRETTY_HEADER = _html_loader('resources/html/pretty_header.html')
HTML_FOOTER = _html_loader('resources/html/footer.html')

DL_BUCKET = sys.argv[1]

s3 = boto3.client('s3')

paginator = s3.get_paginator('list_objects_v2')

response_iterator = paginator.paginate(
    Bucket=DL_BUCKET,
    Prefix='pypi/'
)


packages : Dict = {}
for response in response_iterator:
    for package in response['Contents']:
        res = re.search(r'\/(.*)\/', format(package['Key']))
        if not res:
            continue

        name = res.group(1).lower()

        # Skip the route for the human readable form of the PyPI
        if name == 'pretty':
            continue

        if name not in packages:
            packages[name] = []

        packages[name].append(Path(package['Key']).name)

# Remove not valid packages to not display them
packages_new = packages.copy()
for package_name, package_values in packages.items():
    if len(package_values) < 1:
        packages_new.pop(package_name)
    if len(package_values) == 1:
        if package_values[0] == 'index.html':
            packages_new.pop(package_name)

index = []
index_pretty = []
index.append(HTML_HEADER)
index_pretty.append(HTML_PRETTY_HEADER)
for name in packages_new.keys():
    index.append(f'        <a href="/pypi/{name}/">{name}/</a>')
    index_pretty.append(
        f'        <div><a href="/pypi/{name}">{name}</a><span>Entries: {len(packages_new[name])}</span></div><br>'
        )
index.append(HTML_FOOTER)
index_pretty.append(HTML_FOOTER)

s3.upload_fileobj(BytesIO('\n'.join(index).encode('utf-8')),
                  DL_BUCKET,
                  'pypi/index.html',
                  ExtraArgs={'ACL': 'public-read', 'ContentType':'text/html'})

s3.upload_fileobj(BytesIO('\n'.join(index_pretty).encode('utf-8')),
                  DL_BUCKET,
                  'pypi/pretty/index.html',
                  ExtraArgs={'ACL': 'public-read', 'ContentType':'text/html'})

for name, filenames in packages_new.items():
    index_wheel = []
    index_wheel.append(HTML_HEADER)
    for fn in filenames:
        # Skip HTML source file of the wheel page
        if fn == 'index.html':
            continue
        index_wheel.append(f'<a href="/pypi/{name}/{fn}">{fn}</a><br/>')
    index_wheel.append(HTML_FOOTER)

    s3.upload_fileobj(BytesIO('\n'.join(index_wheel).encode('utf-8')),
                      DL_BUCKET,
                      f'pypi/{name}/index.html',
                      ExtraArgs={'ACL': 'public-read', 'ContentType':'text/html'})
