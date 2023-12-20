#
# SPDX-FileCopyrightText: 2023 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import subprocess
import sys

failed_wheels = 0
succeeded_wheels = 0

with open('dependent_requirements.txt', 'r') as f:
    requirements = f.readlines()

for requirement in requirements:
    out = subprocess.run(
            [f'{sys.executable}', '-m', 'pip', 'wheel', f'{requirement}',
             '--find-links', 'downloaded_wheels', '--wheel-dir', 'downloaded_wheels'],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE
             )

    print(out.stdout.decode('utf-8'))
    if out.stderr:
        print(out.stderr.decode('utf-8'))

    if out.returncode != 0:
        failed_wheels += 1
    else:
        succeeded_wheels += 1


print('---------- STATISTICS ----------')
print(f'Succeeded {succeeded_wheels} wheels')
print(f'Failed {failed_wheels} wheels')
