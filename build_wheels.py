import json
import os
import re
from typing import List
from typing import Optional
from typing import Union

import requests
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement

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

print(f'ENV variables: IDF v{MIN_IDF_MAJOR_VERSION}.{MIN_IDF_MINOR_VERSION}'
      f' -- grater or equal release and master branches will be considered'
      )


def check_response(response: requests.Response, warning: str, exit_on_wrong: bool = False) -> bool:
    """Print warning or exit the script when response code is not correct"""
    if response.status_code == 200:
        return True
    if exit_on_wrong:
        raise SystemExit(f'{warning}\n{response.text}')
    print(warning, '\n')
    return False


# ESP-IDF branches list
def fetch_idf_branches() -> List[str]:
    """Fetch IDF branches from URL specified in global variables"""
    res = requests.get(IDF_BRANCHES_URL, timeout=10)
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
    res = requests.get(IDF_MASTER_VERSION_URL, timeout=10)
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
    print(f'---------- ESP-IDF BRANCH {branch} ----------')
    requirements_txt: List[str] = []

    for feature in idf_requirements_json['features']:
        res = requests.get(f"{IDF_RESOURCES_URL}{branch}/{feature['requirement_path']}", timeout=10)
        if check_response(res, f"Failed to download feature (requirement group) '{feature['name']}'"):
            requirements_txt += res.text.splitlines()
            print(f"Added ESP-IDF {feature['name']} requirements")
    return requirements_txt


def _download_branch_constraints(constraint_file_url: str, branch, idf_constraint: str) -> List[str]:
    """Download constraints file for specific branch"""
    res = requests.get(constraint_file_url, timeout=10)
    if check_response(res, f'Failed to download ESP-IDF constraints file {idf_constraint} for branch {branch}'):
        requirements_txt = res.text.splitlines()
        print(f'Added ESP-IDF constraints file {idf_constraint} for branch {branch}')
        return requirements_txt
    return []


def _add_into_requirements(requirements_txt: List[str]) -> set[Union[Requirement, str]]:
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
                # TODO not a classic requirement (e.g. '--only-binary cryptography') when building wheels
                requirements_set.add(line)
    return requirements_set


def assemble_requirements(idf_branches: List[str], idf_constraints: List[str]) -> set[Union[Requirement, str]]:
    """Assemble IDF requirements into set to prevent duplicates"""
    requirements_txt: List[str] = []

    for i, branch in enumerate(idf_branches):
        idf_requirements_json_url = f'{IDF_RESOURCES_URL}{branch}/tools/requirements.json'
        constraint_file_url = f'https://dl.espressif.com/dl/esp-idf/espidf.constraints.{idf_constraints[i]}.txt'

        res = requests.get(idf_requirements_json_url, timeout=10)
        if not check_response(res, f'\nFailed to download requirements JSON for branch {branch}'):
            continue

        idf_requirements_json = json.loads(res.content)

        requirements_txt += _download_branch_requirements(branch, idf_requirements_json)
        requirements_txt += _download_branch_constraints(constraint_file_url, branch, idf_constraints[i])

    return _add_into_requirements(requirements_txt)


def print_requirements(requirements_set: set[Union[Requirement, str]]):
    """Prints assembled list of requirements"""
    print('\n---------- REQUIREMENTS ----------')
    for req in requirements_set:
        print(req)


def main():
    """Builds Python wheels for IDF dependencies"""
    idf_repo_branches = fetch_idf_branches()
    idf_branches = get_used_idf_branches(idf_repo_branches)
    print(f'ESP-IDF branches to be downloaded requirements for:\n{idf_branches}\n')

    idf_constraints = get_constraints_versions(idf_branches)
    print(f'ESP-IDF constrains files versions to be downloaded requirements for:\n{idf_constraints}\n')

    requirements_set = assemble_requirements(idf_branches, idf_constraints)

    print_requirements(requirements_set)


if __name__ == '__main__':
    main()
