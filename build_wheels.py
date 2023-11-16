import json
import os
import re
import subprocess
from typing import List
from typing import Optional
from typing import Union

import requests
import yaml
from colorama import Fore
from colorama import Style
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
    print(Fore.BLUE + f'---------- ESP-IDF BRANCH {branch} ----------' + Style.RESET_ALL)
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

        res = requests.get(idf_requirements_json_url, timeout=10)
        if not check_response(res, f'\nFailed to download requirements JSON for branch {branch}'):
            continue

        idf_requirements_json = json.loads(res.content)

        requirements_txt += _download_branch_requirements(branch, idf_requirements_json)
        requirements_txt += _download_branch_constraints(constraint_file_url, branch, idf_constraints[i])

    if make_txt_file:
        # TXT file from all downloaded requirements and constraints files
        # useful for debugging or to see the comments for requirements
        with open('requirements.txt', 'w') as f:
            for line in requirements_txt:
                f.write(line)
                f.write('\n')

    return _add_into_requirements(requirements_txt)


# --- exclude_list and include_list ---
def _change_specifier_logic(specifier: str) -> str:
    """Change specifier logic to opposite
        - e.g. "<3" will be ">3"
    """
    #print(f"Original specifier: {specifier}")
    new_spec = specifier
    replacements = {'<': '>',
                    '>': '<',
                    '!': '=',
                    '==': '!='}
    for old, new in replacements.items():
        if old in specifier and '===' not in specifier:
            new_spec = specifier.replace(old, new)
    #print(f"Specifier changed: {new_spec}")
    return new_spec


def yaml_to_requirement(yaml_file:str, exclude: bool = False) -> set:
    """Converts YAML defined package into packaging.requirements Requirement
    which can be directly used with pip
    - when exclude is set to True it makes opposite logic for Requirement to be excluded"""
    with open(yaml_file, 'r') as f:
        yaml_list = yaml.load(f, yaml.Loader)

    exclude_list_set: set[Requirement] = set()

    if not yaml_list:
        return exclude_list_set

    for package in yaml_list:
        req_txt = f"{package['package_name']}"

        if 'version' in package:
            if isinstance(package['version'], list):
                ver_txt = ''
                for ver_str in package['version']:
                    if exclude:
                        ver_txt += f'{_change_specifier_logic(ver_str)},'
                    else:
                        ver_txt += f'{ver_str},'
                req_txt += ver_txt[:-1]
            else:
                if exclude:
                    req_txt += _change_specifier_logic(package['version'])
                else:
                    req_txt += package['version']

        if 'platform' in package:
            if isinstance(package['platform'], list):
                plf_txt = '; '
                for plf_str in package['platform']:
                    if exclude:
                        plf_txt += f"sys_platform != '{plf_str}' or "
                    else:
                        plf_txt += f"sys_platform == '{plf_str}' or "
                req_txt += plf_txt[:-4]
            else:
                if exclude:
                    req_txt += f"; sys_platform != '{package['platform']}'"
                else:
                    req_txt += f"; sys_platform == '{package['platform']}'"

        exclude_list_set.add(Requirement(req_txt))
    return exclude_list_set


def exclude_from_requirements(assembled_requirements:set, exclude_list: set, print_requirements: bool = True) -> set:
    """Exclude packages defined in exclude_list from assembled requirements
        - print_requirements = true will print the changes
    """
    new_assembled_requirements = set()
    not_in_exclude = []
    if print_requirements:
        print(Fore.BLUE + '---------- REQUIREMENTS ----------')

    for requirement in assembled_requirements:
        for req in exclude_list:
            if req.name in requirement.name:

                if requirement.specifier and req.specifier:
                    new_specifier = requirement.specifier & req.specifier
                elif requirement.specifier and not req.specifier:
                    new_specifier = requirement.specifier
                elif not requirement.specifier and req.specifier:
                    new_specifier = req.specifier
                else:
                    new_specifier = ''

                if requirement.marker and req.marker:
                    new_markers = f'({requirement.marker}) and ({req.marker})'
                elif requirement.marker and not req.marker:
                    new_markers = requirement.marker
                elif not requirement.marker and req.marker:
                    new_markers = req.marker
                else:
                    new_markers = ''

                if not req.specifier and not req.marker:
                    if print_requirements:
                        print(Fore.RED + f'-- {requirement}')
                    continue

                if new_markers:
                    new_requirement = Requirement(f'{requirement.name}{new_specifier}; {new_markers}')
                else:
                    new_requirement = Requirement(f'{requirement.name}{new_specifier}')

                new_assembled_requirements.add(new_requirement)

                if print_requirements:
                    print(Fore.RED + f'-- {requirement}')
                    print(Fore.GREEN + f'++ {new_requirement}')
            else:
                not_in_exclude.append(True)
        if len(not_in_exclude) == len(exclude_list):
            if print_requirements:
                print(Style.RESET_ALL + str(requirement))
            new_assembled_requirements.add(requirement)

        not_in_exclude = []

    if print_requirements:
        print(Fore.BLUE + '---------- END OF REQUIREMENTS ----------' + Style.RESET_ALL)

    return new_assembled_requirements


# --- Build wheels ---
def build_wheels(requirements: set):
    """Build Python wheels"""
    dir = f'{os.path.curdir}{(os.sep)}downloaded_wheels'
    for requirement in requirements:
        # --only-binary requirement wheel build
        if non_classic_requirement and non_classic_requirement[0].replace('--only-binary ', '') in requirement.name:
            only_bin = non_classic_requirement[0].replace('--only-binary', '').strip()
            var = subprocess.run(
                ['python', '-m', 'pip', 'wheel', f'{requirement}', '--find-links', f'{dir}',
                 '--wheel-dir', f'{dir}', '--only-binary', f'{only_bin}'],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            print(f'Ret_code: {str(var.returncode)}, std_err: {str(var.stderr)}, std_out: {str(var.stdout)}')

            non_classic_requirement.remove(non_classic_requirement[0])
            continue

        # requirement wheel build
        var = subprocess.run(
            ['python', '-m', 'pip', 'wheel', f'{requirement}', '--find-links', f'{dir}',
             '--wheel-dir', f'{dir}'],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE
             )
        print(f'Ret_code: {str(var.returncode)}, std_err: {str(var.stderr)}, std_out: {str(var.stdout)}')


def main():
    """Builds Python wheels for ESP-IDF dependencies for master and release branches
    grater or equal to specified"""

    idf_repo_branches = fetch_idf_branches()
    idf_branches = get_used_idf_branches(idf_repo_branches)
    print(f'ESP-IDF branches to be downloaded requirements for:\n{idf_branches}\n')

    idf_constraints = get_constraints_versions(idf_branches)
    print(f'ESP-IDF constrains files versions to be downloaded requirements for:\n{idf_constraints}\n')

    requirements = assemble_requirements(idf_branches, idf_constraints)

    exclude_list = yaml_to_requirement('exclude_list.yaml', exclude=True)
    excluded_requirements = exclude_from_requirements(requirements, exclude_list)

    include_list = yaml_to_requirement('include_list.yaml')
    print(Fore.BLUE + '---------- ADDITIONAL REQUIREMENTS ----------' + Style.RESET_ALL)
    for req in include_list:
        print(req)
    print(Fore.BLUE + '---------- END OF ADDITIONAL REQUIREMENTS ----------' + Style.RESET_ALL)

    build_wheels(excluded_requirements)

    build_wheels(include_list)


if __name__ == '__main__':
    main()
