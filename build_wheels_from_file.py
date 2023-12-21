#
# SPDX-FileCopyrightText: 2023 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import argparse
import os
import subprocess
import sys

from colorama import Fore

from _helper_functions import print_color

parser = argparse.ArgumentParser(description='Process build arguments.')
parser.add_argument('requirements_path', metavar='Path', type=str, nargs='+',
                    help='path to Python version dependent requirements txt')

args = parser.parse_args()

requirements_dir = args.requirements_path[0]

failed_wheels = 0
succeeded_wheels = 0

try:
    with open(f'{requirements_dir}{os.sep}dependent_requirements.txt', 'r') as f:
        requirements = f.readlines()
except Exception as e:
    raise SystemExit(f'Python version dependent requirements directory or file not found ({e})')

for requirement in requirements:
    out = subprocess.run(
            [f'{sys.executable}', '-m', 'pip', 'wheel', f'{requirement}',
             '--find-links', 'downloaded_wheels', '--wheel-dir', 'downloaded_wheels'],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE
             )

    print(out.stdout.decode('utf-8'))
    if out.stderr:
        print_color(out.stderr.decode('utf-8'), Fore.RED)

    if out.returncode != 0:
        failed_wheels += 1
    else:
        succeeded_wheels += 1


print_color('---------- STATISTICS ----------')
print_color(f'Succeeded {succeeded_wheels} wheels', Fore.GREEN)
print_color(f'Failed {failed_wheels} wheels', Fore.RED)

if failed_wheels != 0:
    raise SystemExit('One or more wheels failed to build')
