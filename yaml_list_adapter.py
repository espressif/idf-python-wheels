#
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import re

import yaml

from colorama import Fore
from packaging.requirements import Requirement

from _helper_functions import merge_requirements
from _helper_functions import print_color


class YAMLListAdapter:
    """Class for loading list of requirements defined in exclude or include lists (YAML files)
    with conversion method to packaging.requirements Requirement.

    Requirement is used because pip can directly work with this format.

    When YAML file is loaded, the packages with the same names (package duplicates) are combined into one requirement.
    Except when the packages has the different version specified, then it is considered as another requirement.

    The output from this class is a set of requirements (set of Requirement types)
    which can be directly used with pip or further processed.
    Sets are used to avoid exact duplicates and to keep the requirements unique.


    - yaml_list ... list of requirements defined in YAML file
    - exclude   ... boolean to set the logic of the requirements (if True, yaml_list logic is inverted)
    - requirements ... set of requirements (types Requirement) which can be directly used with pip
    -------------------------------------

    ### TERMINOLOGY:

    (YAML file ... Requirement (https://packaging.pypa.io/en/stable/requirements.html#packaging.requirements.Requirement.extras))
    - package_name ... NAME of the package/requirement
    - version      ... (version) SPECIFIER of the package/requirement
    - platform     ... (sys_platform) MARKER for the package/requirement
    - python       ... (python_version) MARKER for the package/requirement

    -------------------------------------

    ### EXAMPLE:
    - package_name: numpy\n
    \tversion: "<1.20"\n
    \tplatform: "win32"\n
    \tpython: ">=3.6"

    -->

        # with exclude=False\n
        Requirement('numpy<1.20; sys_platform == "win32" and python_version >= "3.6"')

        # with exclude=True (to preserve logic for pip another requirement needs to be added)\n
        Requirement('numpy>=1.20; sys_platform == "win32" or python_version >= "3.6"')\n
        Requirement('numpy<1.20; sys_platform != "win32" or python_version < "3.6"')
    """

    yaml_list: list = list()
    exclude: bool = False
    requirements: set = set()

    def __init__(self, yaml_file: str, exclude: bool = False) -> None:
        try:
            with open(yaml_file, "r") as f:
                self._yaml_list = yaml.load(f, yaml.Loader)
        except FileNotFoundError:
            print_color(f"File not found, please check the file: {yaml_file}", Fore.RED)
        self.exclude = exclude

        # Assemble duplicates of requirements/packages with the same name and remove them from the YAML list
        _requirement_duplicates = self._assemble_requirements_duplicates()
        # Convert YAML list to set of requirements without duplicates
        self.requirements = self._yaml_to_requirement(self._yaml_list, exclude=self.exclude)
        # Combine requirements/packages with the same name (duplicates)
        # into one requirement and replaces original requirements
        _combine_package_duplicates(self, _requirement_duplicates)

    def _change_specifier_logic(self, spec_with_text: str) -> tuple:
        """Change specifier logic to opposite
        e.g. "<1.20" will be ">=1.20"
        - this function is used for exclude_list.yaml to change the logic of (version) specifier
        to create opposite logic for Requirement installed by pip

        - return (new_version_specifier, text_after(version number), original_version_specifier)
        """
        pattern = re.compile(r"(===|==|!=|~=|<=?|>=?|===?)\s*(.*)")
        try:
            match = pattern.match(spec_with_text)
            if match:
                str_match: tuple = match.groups()
        except AttributeError:
            print_color(f"Unexpected version specifier: {spec_with_text}", Fore.YELLOW)
            raise SystemExit()

        ver_specifier, text = str_match  # e.g. ('<', '1.20')

        for old, new in (
            ("<", ">="),
            (">", "<="),
            ("<=", ">"),
            (">=", "<"),
            ("!", "="),
            ("~", "!"),
            ("===", "==="),  # not changed specifier for arbitrary equality defined by PEP440
            # (https://packaging.python.org/en/latest/specifications/version-specifiers/#arbitrary-equality)
            ("==", "!="),
        ):
            if old in ver_specifier:
                new_ver_spec = ver_specifier.replace(old, new)
                break
        return (new_ver_spec, text, ver_specifier)

    def _yaml_to_requirement(self, yaml: list, exclude: bool = False) -> set:
        """Converts YAML defined requirement into packaging.requirements Requirement
        which can be directly used with pip.

        Markers (platform and python) are ANDed between and multiple values of the marker are ORed between.

        When exclude is set to True, the logic of the Requirement is changed to be excluded by pip.
        To preserve the logic, another requirement needs to be added
        when exclusion is only for platform or python version.

        -------------------------------------

        ### EXAMPLE for exclude=True:

        - requirement from ESP-IDF is click>=7.0

        #### -- in exclude_list.yaml is defined -->

        - package_name: click\n
        \tversion: ['>8', '==7.2']\n
        \tplatform: "win32"\n

        #### -- the output will be -->

        -- click>=7.0                               # remove requirement\n
        ++ click>=7.0; sys_platform != "win32"      # add requirement constraining platform\n
        ++ click!=7.2,<=8; sys_platform == "win32"  # add requirement constraining version on supported platform

        """
        yaml_list: list = yaml

        requirements_set: set[Requirement] = set()

        if not yaml_list:
            return requirements_set

        for package in yaml_list:
            # get attributes of the package if defined to reduce unnecessary complexity
            package_version = package["version"] if "version" in package else ""
            package_platform = package["platform"] if "platform" in package else ""
            package_python = package["python"] if "python" in package else ""

            requirement_str_list = [f"{package['package_name']}"]

            # if package has version specifier, process it and add to the requirement
            if package_version:
                if not isinstance(package_version, list):
                    new_spec, ver, old_spec = self._change_specifier_logic(package_version)
                    requirement_str_list.append(f"{new_spec}{ver}" if exclude else f"{old_spec}{ver}")

                else:  # list of version specifiers defined
                    version_list = []
                    for elem in package_version:
                        new_spec, ver, old_spec = self._change_specifier_logic(elem)
                        if exclude:
                            version_list.append(f"{new_spec}{ver}")
                        else:
                            version_list.append(f"{old_spec}{ver}")

                    requirement_str_list.append(",".join(version_list))

            # if package has platform markers defined, add it to the requirement
            if package_platform or package_python:
                requirement_str_list.append("; ")

            if package_platform and not package_version:
                if not isinstance(package_platform, list):
                    requirement_str_list.append(
                        (
                            f"sys_platform != '{package_platform}'"
                            if exclude
                            else f"sys_platform == '{package_platform}'"
                        )
                    )

                else:  # list of platforms defined
                    platform_list = [
                        f"sys_platform != '{plf}'" if exclude else f"sys_platform == '{plf}'"
                        for plf in package_platform
                    ]

                    requirement_str_list.append(" or ".join(platform_list))

            if exclude and (package_platform or package_python) and package_version:
                requirement_old_str_list = [f"{package['package_name']}; "]

            if package_platform and package_version:
                if not isinstance(package_platform, list):
                    requirement_str_list.append(f"sys_platform == '{package_platform}'")

                    if exclude:
                        requirement_old_str_list.append(f"sys_platform != '{package_platform}'")

                else:
                    platform_list = [f"sys_platform == '{plf}'" for plf in package_platform]
                    requirement_str_list.append(" or ".join(platform_list))

                    if exclude:
                        platform_list_old = [f"sys_platform != '{plf}'" for plf in package_platform]
                        requirement_old_str_list.append(" or ".join(platform_list_old))

            if package_platform and package_python:
                requirement_str_list.append(" and ")

            if exclude and (package_platform and package_python) and package_version:
                requirement_old_str_list.append(" and ")

            # if package has python markers defined, add it to the requirement
            if package_python and not package_version:
                if not isinstance(package_python, list):
                    new_spec, text_after, old_spec = self._change_specifier_logic(package_python)
                    requirement_str_list.append(
                        (
                            f"python_version {new_spec} '{text_after}'"
                            if exclude
                            else f"python_version {old_spec} '{text_after}'"
                        )
                    )

                else:  # list of python versions defined
                    python_list = []
                    for elem in package_python:
                        new_spec, text_after, old_spec = self._change_specifier_logic(elem)
                        if exclude:
                            python_list.append(f"python_version {new_spec} '{text_after}'")
                        else:
                            python_list.append(f"python_version {old_spec} '{text_after}'")

                    requirement_str_list.append(" and ".join(python_list))

            if package_python and package_version:
                if not isinstance(package_python, list):
                    new_spec, text_after, old_spec = self._change_specifier_logic(package_python)
                    requirement_str_list.append(f"python_version {old_spec} '{text_after}'")

                    if exclude:
                        requirement_old_str_list.append(f"python_version {new_spec} '{text_after}'")

                else:
                    python_list = []
                    python_list_old = []
                    for elem in package_python:
                        new_spec, text_after, old_spec = self._change_specifier_logic(elem)

                        python_list.append(f"python_version {old_spec} '{text_after}'")
                        if exclude:
                            python_list_old.append(f"python_version {new_spec} '{text_after}'")
                    requirement_str_list.append("" + " and ".join(python_list))

                    if exclude:
                        requirement_old_str_list.append(" and ".join(python_list_old))

            if exclude and (package_platform or package_python) and package_version:
                requirements_set.add(Requirement("".join(requirement_old_str_list)))

            requirements_set.add(Requirement("".join(requirement_str_list)))
        return requirements_set

    def _assemble_requirements_duplicates(self):
        """Creates dictionary of requirements with the same requirement/package name for further processing.
        - key is the name of the requirement/package and value is a set of requirements (types Requirement)
        - different version of package is considered as another requirement, not duplicate which is combined

        -------------------------------------
        ### EXAMPLE:
        #### - YAML list defined requirements (exclude_list.yaml)
        #dbus-python can not be build on Windows\n
        - package_name: 'dbus-python'\n
        \tplatform: ['win32']

        #dbus-python can not be build with Python > 3.11 on MacOS\n
        - package_name: 'dbus-python'\n
        \tplatform: 'darwin'\n
        \tpython: '>3.11'

        - package_name: 'dbus-python'\n
        \tplatform: 'linux'

        #### -- will assemble following dictionary (exclude=True) -->
        {'dbus-python': {<Requirement('dbus-python; sys_platform != "darwin" and python_version <= "3.11"')>,
                        <Requirement('dbus-python; sys_platform != "linux"')>}}

        #### - Also removes requirement/package from the YAML list except first occurrence
        """
        duplicates_dict = {}
        for i, requirement in enumerate(self._yaml_list):
            package_name = requirement["package_name"]

            for next_requirement in self._yaml_list[i + 1 :]:
                if next_requirement["package_name"] == package_name and next_requirement != requirement:
                    if package_name not in duplicates_dict:
                        duplicates_dict[package_name] = set()

                    if "version" in next_requirement:
                        # Different version of package is not considered as duplicate, but new requirement
                        continue

                    duplicates_dict[package_name].add(
                        list(self._yaml_to_requirement([next_requirement.copy()], self.exclude))[0]
                    )
                    self._yaml_list.remove(next_requirement)

        return duplicates_dict


def _combine_package_duplicates(self, requirement_duplicates: dict):
    """Combines requirements/packages of the YAMLListAdapter with the requirement/package
    (duplicates from assembled dict) and replaces the original requirements set.

    -------------------------------------

    ### EXAMPLE:
    #### - YAML list defined requirements (exclude_list.yaml)
    #dbus-python can not be build on Windows\n
    - package_name: 'dbus-python'\n
    \tplatform: ['win32']

    #dbus-python can not be build with Python > 3.11 on MacOS\n
    - package_name: 'dbus-python'\n
    \tplatform: 'darwin'\n
    \tpython: '>3.11'

    - package_name: 'dbus-python'\n
    \tplatform: 'linux'

    #### - Assembled duplicates dictionary (exclude=True)
    {'dbus-python': {<Requirement('dbus-python; sys_platform != "darwin" and python_version <= "3.11"')>,
                    <Requirement('dbus-python; sys_platform != "linux"')>}}

    #### -- will replace original requirement with following (exclude=True) -->
    dbus-python;sys_platform == "linux" and (
        (sys_platform != "win32" and (
            sys_platform != "darwin" and python_version <= "3.11")) and sys_platform != "linux")

    #### - directly used with pip constraining the installation of dbus-python defined in exclude_list.yaml
    """
    new_requirements: set = set()
    for requirement in self.requirements:
        if requirement.name in requirement_duplicates:
            # empty strings for new version specifier and marker because it is added every time to new requirement
            for duplicate in requirement_duplicates[requirement.name]:
                # rewrite requirement to continuously merge any following duplicate
                requirement = merge_requirements(requirement, duplicate)  # new_requirement
        # add new requirement or unchanged requirement to the set of requirements
        new_requirements.add(requirement)
    # replace original requirements with new requirements
    self.requirements = new_requirements
