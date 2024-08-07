#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import json
import os
import re
import subprocess
import sys
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import requests
from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement

from _helper_functions import merge_requirements
from _helper_functions import print_color
from yaml_list_adapter import YAMLListAdapter

# GLOBAL VARIABLES
# URL to fetch IDF branches from
IDF_BRANCHES_URL = 'https://api.github.com/repos/espressif/esp-idf/branches?protected=true&per_page=100'
# URL to download constraints file from (vX.Y.txt part is auto-completed)
IDF_CONSTRAINTS_URL = 'https://dl.espressif.com/dl/esp-idf/espidf.constraints.'
# URL for IDF 'resources' root directory for requirements paths
IDF_RESOURCES_URL = 'https://raw.githubusercontent.com/espressif/esp-idf/'
# URL for IDF master CMAKE version file
IDF_MASTER_VERSION_URL = f'{IDF_RESOURCES_URL}master/tools/cmake/version.cmake'

# Minimal IDF release version to take requirements from (v{MAJOR}.{MINOR})
# Requirements from all release branches and master equal or above this will be considered
# Specified in Github variables
MIN_IDF_MAJOR_VERSION: int = int(os.environ.get('MIN_IDF_MAJOR_VERSION', '5'))
MIN_IDF_MINOR_VERSION: int = int(os.environ.get('MIN_IDF_MINOR_VERSION', '0'))

# GH token for the authenticated requests to extend the limit
GH_TOKEN: str = os.environ.get('GH_TOKEN', '')

# Authentication header
AUTH_HEADER: Dict[str, str] = {
    'authorization': f'Bearer {GH_TOKEN}',
    'content-type': 'application/json'
}

print(f'ENV variables: IDF v{MIN_IDF_MAJOR_VERSION}.{MIN_IDF_MINOR_VERSION}'
      f' -- grater or equal release and master branches will be considered'
      )


def check_response(response: requests.Response, warning: str, exit_on_wrong: bool = False) -> bool:
    """Print warning or exit the script when response code is not correct"""
    if response.status_code == 200:
        return True
    if exit_on_wrong:
        raise SystemExit(f'{warning}\n{response.text}')
    print_color(f'{warning}\n', Fore.LIGHTRED_EX)
    return False


# ESP-IDF branches list
def fetch_idf_branches() -> List[str]:
    """Fetch IDF branches from URL specified in global variables"""
    res = requests.get(IDF_BRANCHES_URL, headers=AUTH_HEADER, timeout=10)
    if check_response(res, 'Failed to fetch ESP-IDF branches.', True):
        return [branch['name'] for branch in res.json()]
    return []


def get_used_idf_branches(idf_repo_branches: List[str]) -> List[str]:
    """Take only IDF master and release branches, only equal or grater version specified in Github variables"""
    idf_branches: List[str] = []
    for branch in idf_repo_branches:
        idf_release = re.match(r'release/v(\d+)\.(\d+)', branch)

        if not idf_release:
            continue

        idf_major, idf_minor = map(int, idf_release.groups())

        if (idf_major, idf_minor) < (MIN_IDF_MAJOR_VERSION, MIN_IDF_MINOR_VERSION):
            continue

        idf_branches.append(branch)

    idf_branches.append('master')
    return idf_branches


# Constraints files versions list
def _idf_version_from_cmake() -> Optional[dict]:
    """Get IDF master branch version from version.cmake"""
    res = requests.get(IDF_MASTER_VERSION_URL, headers=AUTH_HEADER, timeout=10)
    if check_response(res, 'Failed to get master version of IDF from CMAKE.'):
        regex = re.compile(r'^\s*set\s*\(\s*IDF_VERSION_([A-Z]{5})\s+(\d+)')
        lines = res.text.splitlines()

        idf_master_ver: dict = {}
        for line in lines:
            ver = regex.match(line)
            if ver:
                idf_master_ver[ver.group(1)] = ver.group(2)
        return idf_master_ver
    return None


def get_constraints_versions(idf_branches: List[str]) -> List[str]:
    """From desired branches passed in get constraints files versions list
    - when branch is not 'release' (without version) it is supposed to be 'master'
    and auto version mechanism is applied if not specified in Github variables or manual workflow not to
    """
    idf_constraints: List[str] = []

    for branch in idf_branches:
        # Handle release branches
        if 'release/' in branch:
            idf_constraints.append(branch.split('release/')[1])
            continue

        # Handle master branch
        idf_master_ver = _idf_version_from_cmake()

        # when IDF version not set correctly and CMAKE version for master is not downloaded
        if idf_branches[0] == 'master' and idf_master_ver is None:
            idf_constraints.append('None')
            continue

        if idf_master_ver is not None:
            next_master_version = f'v{idf_master_ver["MAJOR"]}.{idf_master_ver["MINOR"]}'
            idf_constraints.append(next_master_version)

    return idf_constraints


# --- Download all requirements from all the branches requirements and constraints files --- #
def _download_branch_requirements(branch: str, idf_requirements_json: dict) -> List[str]:
    """Download requirements files for all groups specified in IDF requirements.JSON"""
    print_color(f'---------- ESP-IDF BRANCH {branch} ----------')
    requirements_txt: List[str] = []

    for feature in idf_requirements_json['features']:
        res = requests.get(
            f"{IDF_RESOURCES_URL}{branch}/{feature['requirement_path']}", headers=AUTH_HEADER, timeout=10
            )
        if check_response(res, f"Failed to download feature (requirement group) '{feature['name']}'"):
            requirements_txt += res.text.splitlines()
            print(f"Added ESP-IDF {feature['name']} requirements")
    return requirements_txt


def _download_branch_constraints(constraint_file_url: str, branch, idf_constraint: str) -> List[str]:
    """Download constraints file for specific branch"""
    res = requests.get(constraint_file_url, headers=AUTH_HEADER, timeout=10)
    if check_response(res, f'Failed to download ESP-IDF constraints file {idf_constraint} for branch {branch}'):
        requirements_txt = res.text.splitlines()
        print(f'Added ESP-IDF constraints file {idf_constraint} for branch {branch}')
        return requirements_txt
    return []


non_classic_requirement:List[str] = []
def _add_into_requirements(requirements_txt: List[str]) -> set:
    """Create set of requirements from downloaded lines of requirements
        - set is used to prevent duplicates
    """
    requirements_set: set[Union[Requirement, str]] = set()
    for line in map(str.strip, requirements_txt):
        # check if in the line or the line itself is not a comment
        line = line.split('#', 1)[0]
        if line:
            try:
                requirements_set.add(Requirement(line))
            except InvalidRequirement:
                # Non classic requirement (e.g. '--only-binary cryptography')
                non_classic_requirement.append(line)
    return requirements_set


def assemble_requirements(idf_branches: List[str], idf_constraints: List[str], make_txt_file:bool=False) -> set:
    """Assemble IDF requirements into set to prevent duplicates"""
    requirements_txt: List[str] = []

    for i, branch in enumerate(idf_branches):
        idf_requirements_json_url = f'{IDF_RESOURCES_URL}{branch}/tools/requirements.json'
        constraint_file_url = f'https://dl.espressif.com/dl/esp-idf/espidf.constraints.{idf_constraints[i]}.txt'

        res = requests.get(idf_requirements_json_url, headers=AUTH_HEADER, timeout=10)
        if not check_response(res, f'\nFailed to download requirements JSON for branch {branch}'):
            continue

        idf_requirements_json = json.loads(res.content)

        requirements_txt += _download_branch_requirements(branch, idf_requirements_json)
        requirements_txt += _download_branch_constraints(constraint_file_url, branch, idf_constraints[i])

    if make_txt_file:
        # TXT file from all downloaded requirements and constraints files
        # useful for debugging or to see the comments for requirements
        with open('requirements.txt', 'w') as f:
            f.write('\n'.join(requirements_txt))

    return _add_into_requirements(requirements_txt)


def exclude_from_requirements(assembled_requirements:set, exclude_list: set, print_requirements: bool = True) -> set:
    """Exclude packages defined in exclude_list from assembled requirements
        - print_requirements = true will print the changes
    """
    new_assembled_requirements = set()
    not_in_exclude = []
    if print_requirements:
        print_color('---------- REQUIREMENTS ----------')

    for requirement in assembled_requirements:
        printed = False
        for req_to_exclude in exclude_list:
            if req_to_exclude.name not in requirement.name:
                not_in_exclude.append(True)
            else:
                if not req_to_exclude.specifier and not req_to_exclude.marker:
                    # Delete requirement
                    if print_requirements:
                        print_color(f'-- {requirement}', Fore.RED)
                    continue

                # Merge requirement and requirement_from_exclude list
                new_requirement = merge_requirements(requirement, req_to_exclude)
                new_assembled_requirements.add(new_requirement)

                if print_requirements:
                    if not printed:
                        print_color(f'-- {requirement}', Fore.RED)
                        printed = True
                    print_color(f'++ {new_requirement}', Fore.GREEN)

        # Add back unchanged requirement
        if len(not_in_exclude) == len(exclude_list):
            if print_requirements:
                print(str(requirement))
            new_assembled_requirements.add(requirement)

        not_in_exclude.clear()

    if print_requirements:
        print_color('---------- END OF REQUIREMENTS ----------')

    return new_assembled_requirements


# --- Build wheels ---
def build_wheels(requirements: set, local_links:bool = True) -> dict:
    """Build Python wheels
        - 'failed' - failed wheels counter
        - 'succeeded' - succeeded wheels counter
    """
    failed_wheels = 0
    succeeded_wheels = 0

    dir = f'{os.path.curdir}{(os.sep)}downloaded_wheels'
    for requirement in requirements:
        # non classic requirement wheel build
        if non_classic_requirement:
            pattern = re.compile(r'(--[^ ]*)(.*)')
            match = pattern.search(non_classic_requirement[0])
            if match:
                argument = match.group(1).strip()
                arg_param = match.group(2).strip()
            if arg_param in requirement.name:
                out = subprocess.run(
                    [f'{sys.executable}', '-m', 'pip', 'wheel', f'{requirement}',
                    '--find-links', f'{dir}', '--wheel-dir', f'{dir}',
                    f'{argument}', f'{arg_param}'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                print(out.stdout.decode('utf-8'))
                if out.stderr:
                    print_color(out.stderr.decode('utf-8'), Fore.RED)
                non_classic_requirement.remove(non_classic_requirement[0])
                continue

        # requirement wheel build
        out = subprocess.run(
            [f'{sys.executable}', '-m', 'pip', 'wheel', f'{requirement}',
             '--find-links', f'{dir}', '--wheel-dir', f'{dir}'],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE
             )

        print(out.stdout.decode('utf-8'))
        if out.stderr:
            print_color(out.stderr.decode('utf-8'), Fore.RED)

        if out.returncode != 0:
            failed_wheels += 1
        else:
            succeeded_wheels += 1

    return {'failed': failed_wheels, 'succeeded': succeeded_wheels}


def get_python_dependent_wheels(wheel_dir:str, requirements:set) -> set:
    """Get Python dependent requirements from downloaded wheel directory"""
    dependent_wheels_set = set()
    dependent_requirements_set = set()

    file_names = os.listdir(wheel_dir)

    # find dependent wheels in wheel directory
    for wheel in file_names:
        pattern = re.compile(r'([^ -]*)-(\d+(\.\d+)*).*?(cp\d+)')
        match = pattern.search(wheel)
        if match is not None:
            wheel_name = match.group(1)
            wheel_version = match.group(2)
            build = match.group(3)

            dependent_wheels_set.add((wheel_name, wheel_version, build))

    # find dependent wheel in requirements
    for name, version, _ in dependent_wheels_set:
        for requirement in requirements:
            if requirement.marker:
                if 'python_version' in str(requirement.marker):
                    # add python version specific requirements from all branches
                    dependent_requirements_set.add(requirement)

            if name.lower() == requirement.name.lower():
                # add requirements with markers
                dependent_requirements_set.add(requirement)
            else:
                # add downloaded and already built requirements (all dependencies)
                dependent_requirements_set.add(Requirement(f'{name}=={version}'))

    return dependent_requirements_set


def main() -> int:
    """Builds Python wheels for ESP-IDF dependencies for master and release branches
    grater or equal to specified"""

    idf_repo_branches = fetch_idf_branches()
    idf_branches = get_used_idf_branches(idf_repo_branches)
    print(f'ESP-IDF branches to be downloaded requirements for:\n{idf_branches}\n')

    idf_constraints = get_constraints_versions(idf_branches)
    print(f'ESP-IDF constrains files versions to be downloaded requirements for:\n{idf_constraints}\n')

    requirements = assemble_requirements(idf_branches, idf_constraints, True)

    exclude_list = YAMLListAdapter('exclude_list.yaml', exclude=True).requirements

    after_exclude_requirements = exclude_from_requirements(requirements, exclude_list)

    include_list = YAMLListAdapter('include_list.yaml').requirements
    print_color('---------- ADDITIONAL REQUIREMENTS ----------')
    for req in include_list:
        print(req)
    print_color('---------- END OF ADDITIONAL REQUIREMENTS ----------')

    print_color('---------- BUILD ADDITIONAL WHEELS ----------')
    additional_whl = build_wheels(include_list)
    failed_wheels = additional_whl['failed']
    succeeded_wheels = additional_whl['succeeded']

    print_color('---------- BUILD WHEELS ----------')
    standard_whl = build_wheels(after_exclude_requirements)
    failed_wheels += standard_whl['failed']
    succeeded_wheels += standard_whl['succeeded']

    print_color('---------- STATISTICS ----------')
    print_color(f'Succeeded {succeeded_wheels} wheels', Fore.GREEN)
    print_color(f'Failed {failed_wheels} wheels', Fore.RED)

    if failed_wheels != 0:
        raise SystemExit('One or more wheels failed to build')

    print_color('---------- PYTHON VERSION DEPENDENT ----------')
    dependent_wheels = get_python_dependent_wheels(f'{os.path.curdir}{(os.sep)}downloaded_wheels',
                                                   after_exclude_requirements)
    after_exclude_dependent_wheels = exclude_from_requirements(dependent_wheels, exclude_list)

    with open('dependent_requirements.txt', 'w') as f:
        for wheel in after_exclude_dependent_wheels:
            f.write(f'{str(wheel)}\n')

    return 0


if __name__ == '__main__':
    main()
