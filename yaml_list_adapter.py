#
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from __future__ import annotations

import re

from pathlib import Path
from typing import Optional
from typing import Set

import yaml

from colorama import Fore
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import exclude_entry_applies_to_platform
from _helper_functions import merge_requirements
from _helper_functions import print_color

# Map runner platforms to sys.platform (pip markers only know win32/linux/darwin)
SYS_PLATFORM_MAP = {
    "windows": "win32",
    "macos": "darwin",
    "linux_armv7": "linux",
    "linux_arm64": "linux",
    "linux_x86_64": "linux",
    "macos_arm64": "darwin",
    "macos_x86_64": "darwin",
}


def _platform_for_marker(platform):
    """Normalize platform to sys_platform value for pip markers."""
    return SYS_PLATFORM_MAP.get(platform, platform)


def invert_version_specifier(spec_with_text: str) -> tuple[str, str, str]:
    """Return the opposite version specifier, its version text, and the original operator.

    ``===`` stays unchanged. A compatible release (``~=``) becomes ``!=``.
    """
    match = re.match(r"(===|==|!=|~=|<=?|>=?)\s*(.*)", spec_with_text)
    if not match:
        print_color(f"Unexpected version specifier: {spec_with_text}", Fore.YELLOW)
        raise SystemExit()
    operator, version = match.groups()
    for original, inverted in (
        ("===", "==="),
        ("<=", ">"),
        (">=", "<"),
        ("==", "!="),
        ("!=", "=="),
        ("~=", "!="),
        ("<", ">="),
        (">", "<="),
    ):
        if original in operator:
            return inverted, version, operator
    print_color(f"Unexpected version specifier: {spec_with_text}", Fore.YELLOW)
    raise SystemExit()


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

    def __init__(self, yaml_file: str, exclude: bool = False, current_platform: Optional[str] = None) -> None:
        self._yaml_list: list = []
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            print_color(f"File not found, please check the file: {yaml_file}", Fore.RED)
            raw = None
        if isinstance(raw, dict):
            # Optional mapping root (e.g. includes under ``includes``); list root is still supported.
            self._yaml_list = raw.get("includes") or raw.get("include_packages") or []
        elif isinstance(raw, list):
            self._yaml_list = raw
        elif raw is not None:
            print_color(f"Unexpected YAML root type in {yaml_file}: {type(raw).__name__}", Fore.RED)
            self._yaml_list = []
        self.exclude = exclude

        # When building wheels: only exclude entries that apply to this platform
        if current_platform and self._yaml_list:
            self._yaml_list = [e for e in self._yaml_list if exclude_entry_applies_to_platform(e, current_platform)]

        # Positive exclusion rows are independent alternatives. The duplicate
        # merger is only valid for the inverted requirements passed to pip.
        _requirement_duplicates = self._assemble_requirements_duplicates() if self.exclude else {}
        self.requirements = self._yaml_to_requirement(self._yaml_list, exclude=self.exclude)
        if self.exclude:
            _combine_package_duplicates(self, _requirement_duplicates)
        # A versioned row rewrites only its own pin. Whole-package rows for the same name
        # still keep the old pin, including where that version is excluded.
        if self.exclude:
            for package in self._yaml_list:
                if "version" not in package or not (package.get("platform") or package.get("python")):
                    continue
                bare = {key: value for key, value in package.items() if key != "version"}
                outside = next(iter(self._yaml_to_requirement([bare], exclude=True))).marker
                if outside is None:
                    continue
                # Complements of this versioned row keep the incoming pin. Other whole-package
                # rows have no pin, so pip would install the newest release outside the exclusion.
                complements = {
                    requirement.marker
                    for requirement in self._yaml_to_requirement([package], exclude=True)
                    if not requirement.specifier
                }
                version = package["version"] if isinstance(package["version"], str) else ""
                package_name = canonicalize_name(package["package_name"])
                updated: set[Requirement] = set()
                for requirement in self.requirements:
                    if canonicalize_name(requirement.name) != package_name or requirement.specifier:
                        updated.add(requirement)
                        continue
                    marker = f"({requirement.marker}) and ({outside})" if requirement.marker else str(outside)
                    pin = ""
                    if version and package.get("platform") and requirement.marker not in complements:
                        pin = version
                    updated.add(Requirement(f"{requirement.name}{pin}; {marker}"))
                self.requirements = updated

    def _change_specifier_logic(self, spec_with_text: str) -> tuple:
        """Change specifier logic to opposite, e.g. ``<1.20`` becomes ``>=1.20``."""
        return invert_version_specifier(spec_with_text)

    def _python_version_marker_fragment_no_package_version(self, package_python, exclude: bool) -> str:
        """Build ``python_version ...`` marker fragment from YAML ``python`` when there is no package ``version``.

        If ``python`` is a list, each element becomes a fragment and fragments are joined with **or**.
        """
        if not isinstance(package_python, list):
            new_spec, text_after, old_spec = self._change_specifier_logic(package_python)
            spec = new_spec if exclude else old_spec
            return f"python_version {spec} '{text_after}'"
        parts = []
        for elem in package_python:
            new_spec, text_after, old_spec = self._change_specifier_logic(elem)
            spec = new_spec if exclude else old_spec
            parts.append(f"python_version {spec} '{text_after}'")
        joined = (" and " if exclude else " or ").join(parts)
        return f"({joined})" if len(parts) > 1 else joined

    def _yaml_to_requirement(self, yaml: list, exclude: bool = False) -> set:
        """Converts YAML defined requirement into packaging.requirements Requirement
        which can be directly used with pip.

        Within one YAML row, ``platform`` and ``python`` are combined with **and**. Multiple entries in a
        **platform** list are **or**ed (match any listed platform). Multiple entries in a **python** list
        are **or**ed (match any listed python constraint).
        For exclude=True **without** a package ``version``, platform + python mean “exclude on this OS **and**
        this Python” (intersection): the keep-marker is ``(sys_platform != p or <inverted python>)`` per
        platform, ANDed across listed platforms (De Morgan). Rows **with** a package version keep the
        split-requirement behaviour documented below.

        A list of equality-only package versions is an OR condition for positive
        rules. Other version lists remain comma-joined intersections.

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

        requirements_set: Set[Requirement] = set()

        if not yaml_list:
            return requirements_set

        for package in yaml_list:
            # get attributes of the package if defined to reduce unnecessary complexity
            package_version = package["version"] if "version" in package else ""
            raw_platform = package["platform"] if "platform" in package else ""
            # Map linux_armv7/arm64/x86_64 to "linux" for pip markers, preserving str | list[str]
            if isinstance(raw_platform, str) and raw_platform:
                package_platform = _platform_for_marker(raw_platform)
            elif isinstance(raw_platform, list) and raw_platform:
                package_platform = list(dict.fromkeys(_platform_for_marker(p) for p in raw_platform))
            else:
                package_platform = ""
            package_python = package["python"] if "python" in package else ""

            # Intersection exclude: "drop on (platform ∈ P) ∧ (python matches)" without a package version.
            # Previous AND of inverted markers wrongly dropped e.g. Linux + same Python.
            if exclude and package_platform and package_python and not package_version:
                py_frag = self._python_version_marker_fragment_no_package_version(package_python, exclude=True)
                plfs = list(package_platform) if isinstance(package_platform, list) else [package_platform]
                terms = [f"(sys_platform != '{plf}' or ({py_frag}))" for plf in plfs]
                requirements_set.add(Requirement(f"{package['package_name']}; " + " and ".join(terms)))
                continue

            requirement_str_list = [f"{package['package_name']}"]

            # if package has version specifier, process it and add to the requirement
            equality_version_alternatives: list[str] = []
            if package_version:
                if not isinstance(package_version, list):
                    new_spec, ver, old_spec = self._change_specifier_logic(package_version)
                    requirement_str_list.append(f"{new_spec}{ver}" if exclude else f"{old_spec}{ver}")

                else:  # list of version specifiers defined
                    version_list = []
                    for elem in package_version:
                        new_spec, ver, old_spec = self._change_specifier_logic(elem)
                        if not exclude and old_spec in ("==", "==="):
                            equality_version_alternatives.append(f"{old_spec}{ver}")
                        elif exclude:
                            version_list.append(f"{new_spec}{ver}")
                        else:
                            version_list.append(f"{old_spec}{ver}")

                    if version_list:
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

                    requirement_str_list.append(f"({' or '.join(platform_list)})")

            if exclude and (package_platform or package_python) and package_version:
                requirement_old_str_list = [f"{package['package_name']}; "]

            if package_platform and package_version:
                if not isinstance(package_platform, list):
                    requirement_str_list.append(f"sys_platform == '{package_platform}'")

                    if exclude:
                        requirement_old_str_list.append(f"sys_platform != '{package_platform}'")

                else:
                    platform_list = [f"sys_platform == '{plf}'" for plf in package_platform]
                    requirement_str_list.append(f"({' or '.join(platform_list)})")

                    if exclude:
                        platform_list_old = [f"sys_platform != '{plf}'" for plf in package_platform]
                        requirement_old_str_list.append(" or ".join(platform_list_old))

            if package_platform and package_python:
                requirement_str_list.append(" and ")

            if exclude and (package_platform and package_python) and package_version:
                requirement_old_str_list.append(" and ")

            # if package has python markers defined, add it to the requirement
            if package_python and not package_version:
                requirement_str_list.append(
                    self._python_version_marker_fragment_no_package_version(package_python, exclude)
                )

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
                    requirement_str_list.append(f"({' or '.join(python_list)})")

                    if exclude:
                        requirement_old_str_list.append(" and ".join(python_list_old))

            if exclude and (package_platform or package_python) and package_version:
                requirements_set.add(Requirement("".join(requirement_old_str_list)))

            if equality_version_alternatives:
                for version in equality_version_alternatives:
                    parts = list(requirement_str_list)
                    parts.insert(1, version)
                    requirements_set.add(Requirement("".join(parts)))
            else:
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


class ExcludeRequirements(set):
    """Positive exclude requirements, with one filtered view per runner platform."""

    def __init__(
        self,
        requirements: set[Requirement],
        by_platform: dict[str, set[Requirement]] | None = None,
    ) -> None:
        super().__init__(requirements)
        self.by_platform = by_platform or {}


def load_exclude_rules(
    path: str | Path = "exclude_list.yaml",
    current_platform: str | None = None,
) -> ExcludeRequirements:
    """Load exclude_list.yaml as positive requirements (``exclude=False``)."""
    if not Path(path).is_file():
        raise FileNotFoundError(f"Exclude rule file not found: {path}")
    if current_platform:
        requirements = YAMLListAdapter(str(path), exclude=False, current_platform=current_platform).requirements
        return ExcludeRequirements(requirements, {current_platform: requirements})

    platforms = (
        "windows",
        "macos_x86_64",
        "macos_arm64",
        "linux_x86_64",
        "linux_arm64",
        "linux_armv7",
    )
    by_platform = {
        platform: YAMLListAdapter(str(path), exclude=False, current_platform=platform).requirements
        for platform in platforms
    }
    return ExcludeRequirements(set().union(*by_platform.values()), by_platform)


def wholly_excluded_names(
    rules: set[Requirement] | list[Requirement] | tuple[Requirement, ...],
) -> set[str]:
    """Packages excluded with no version, platform, or Python limit."""
    return {canonicalize_name(rule.name) for rule in rules if not rule.specifier and rule.marker is None}


def is_excluded(
    name: str,
    version: str,
    platform: str,
    python_version: str,
    rules: set[Requirement] | list[Requirement] | tuple[Requirement, ...],
) -> bool:
    """Return whether ``name==version`` matches a positive exclude requirement."""
    for requirement in applicable_exclusion_rules(name, platform, python_version, rules):
        if not requirement.specifier or requirement.specifier.contains(version, prereleases=True):
            return True
    return False


def marker_environment(platform: str, python_version: str) -> dict[str, str]:
    """Build the PEP 508 marker environment shared by exclusion consumers."""
    return {
        "sys_platform": _platform_for_marker(platform),
        "python_version": python_version,
    }


def applicable_exclusion_rules(
    name: str,
    platform: str,
    python_version: str,
    rules: set[Requirement] | list[Requirement] | tuple[Requirement, ...],
) -> list[Requirement]:
    """Return rules for ``name`` whose platform/Python marker matches."""
    package_name = canonicalize_name(name)
    requirements = rules.by_platform.get(platform, rules) if isinstance(rules, ExcludeRequirements) else rules
    environment = marker_environment(platform, python_version)
    return [
        requirement
        for requirement in requirements
        if canonicalize_name(requirement.name) == package_name
        and (requirement.marker is None or requirement.marker.evaluate(environment))
    ]


def _exclude_bound(adapter: YAMLListAdapter, name: str, spec_text: str) -> str | None:
    """Return one inverted bound from ``YAMLListAdapter`` exclude mode.

    A row with several bounds excludes their intersection. Each bound is inverted
    on its own; joining the inverses with a comma would reject every version.
    """
    inverted = next(
        iter(
            adapter._yaml_to_requirement(
                [{"package_name": name, "version": spec_text}],
                exclude=True,
            )
        )
    )
    if inverted.specifier == Requirement(f"{name}{spec_text}").specifier:
        return None
    return str(inverted)


def exclusion_constraint_alternatives(
    platform: str,
    python_version: str,
    rules: ExcludeRequirements,
) -> list[list[str]]:
    """Return constraint sets that avoid versioned exclusions.

    One bound becomes one constraint. Several bounds on one row exclude a range,
    and the complement of a range is an alternative (``<1`` or ``>=2``), not both
    at once. Pip would otherwise intersect the inverted bounds and reject every version.
    """
    adapter = YAMLListAdapter.__new__(YAMLListAdapter)
    mandatory: list[str] = []
    choices: list[list[str]] = []
    for rule in rules.by_platform.get(platform, rules):
        if rule.marker is not None and not rule.marker.evaluate(marker_environment(platform, python_version)):
            continue
        bounds = [
            bound
            for specifier in rule.specifier
            if (bound := _exclude_bound(adapter, rule.name, f"{specifier.operator}{specifier.version}")) is not None
        ]
        if len(bounds) == 1:
            mandatory.append(bounds[0])
        elif bounds:
            choices.append(bounds)
    if not choices:
        return [mandatory]
    groups: list[list[str]] = [[]]
    for options in choices:
        groups = [group + [option] for group in groups for option in options]
    return [mandatory + group for group in groups]


def exclusion_constraints(
    platform: str,
    python_version: str,
    rules: ExcludeRequirements,
) -> list[str]:
    """Return constraints shared by every alternative for this environment.

    A range exclusion has no single constraint that allows every remaining version.
    Callers that need those versions use ``exclusion_constraint_alternatives``.
    """
    groups = exclusion_constraint_alternatives(platform, python_version, rules)
    if not groups:
        return []
    common = set(groups[0])
    for group in groups[1:]:
        common &= set(group)
    return [item for item in groups[0] if item in common]
