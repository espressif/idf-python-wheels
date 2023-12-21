#
# SPDX-FileCopyrightText: 2023 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from colorama import Fore
from colorama import Style


def print_color(text:str, color:str = Fore.BLUE):
    """Print colored text specified by color argument based on colorama
        - default color BLUE
    """
    print(f'{color}', f'{text}', Style.RESET_ALL)
