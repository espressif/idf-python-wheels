#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from colorama import Fore
from colorama import Style
from packaging.requirements import Requirement


def print_color(text:str, color:str = Fore.BLUE):
    """Print colored text specified by color argument based on colorama
        - default color BLUE
    """
    print(f'{color}', f'{text}', Style.RESET_ALL)


def merge_requirements(requirement:Requirement, another_req:Requirement) -> Requirement:
    """Merges two requirements into one requirement."""
    new_ver_specifier = ''
    new_markers = ''
    if requirement.specifier and another_req.specifier:
        if not another_req.marker and ('==' not in str(requirement.specifier)
                                       and '!=' not in str(requirement.specifier)):
            new_ver_specifier = f'{requirement.specifier},{another_req.specifier}'
        else:
            new_ver_specifier = another_req.specifier
    elif requirement.specifier and not another_req.specifier:
        new_ver_specifier = requirement.specifier
    elif not requirement.specifier and another_req.specifier:
        new_ver_specifier = another_req.specifier

    if requirement.marker and another_req.marker:
        new_markers = f'({requirement.marker}) and ({another_req.marker})'
    elif requirement.marker and not another_req.marker:
        new_markers = requirement.marker
    elif not requirement.marker and another_req.marker:
        new_markers = another_req.marker

    # construct new requirement
    new_requirement = Requirement(
        f'{requirement.name}{new_ver_specifier}' + (f'; {new_markers}' if new_markers else '')
    )

    return new_requirement
