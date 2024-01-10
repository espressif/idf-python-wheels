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

HTML_HEADER = '''
<!DOCTYPE html>
<html>
  <head>
    <meta name="pypi:repository-version" content="1.0">
    <title>Simple index</title>
  </head>
  <body>
'''

HTML_FOOTER = '''
</body>
</html>
'''

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
        #continue when package == index.html
        if not res:
            continue

        name = res.group(1).lower()
        if name not in packages:
            packages[name] = []

        packages[name].append(Path(package['Key']).name)

index = []
index.append(HTML_HEADER)
for name in packages.keys():
    index.append(f'<a href="/pypi/{name}/">{name}/</a>')
index.append(HTML_FOOTER)

s3.upload_fileobj(BytesIO('\n'.join(index).encode('utf-8')),
                  DL_BUCKET,
                  'pypi/index.html',
                  ExtraArgs={'ACL': 'public-read', 'ContentType':'text/html'})

for name, filenames in packages.items():
    index_wheel = []
    index_wheel.append(HTML_HEADER)
    for fn in filenames:
        index_wheel.append(f'<a href="/pypi/{name}/{fn}">{fn}</a><br/>')
    index_wheel.append(HTML_FOOTER)

    s3.upload_fileobj(BytesIO('\n'.join(index).encode('utf-8')),
                      DL_BUCKET,
                      f'pypi/{name}/index.html',
                      ExtraArgs={'ACL': 'public-read', 'ContentType':'text/html'})
