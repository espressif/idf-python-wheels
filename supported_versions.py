# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
import json
import sys

from datetime import datetime

import requests

from dateutil.relativedelta import relativedelta

from _helper_functions import print_color

try:
    IDF_VERSIONS_TXT = str(sys.argv[1])
except IndexError:
    raise SystemExit("Error: IDF versions txt not provided.")

json_data = {"supported_idf": [], "oldest_supported_idf": "", "supported_python": [], "oldest_supported_python": ""}


def get_supported_idf_versions():
    """
    Fetches the supported versions of the ESP-IDF from the official Espressif's server.
    Returns a list of version strings.
    """
    url = IDF_VERSIONS_TXT
    try:
        response = requests.get(url)
        response.raise_for_status()
        supported_idf_versions = response.text.splitlines()
    except requests.RequestException as exc:
        raise SystemExit(f"Failed to fetch supported ESP-IDF versions.\nError: {exc}")

    supported_idf_versions = [version for version in supported_idf_versions if version.startswith("v")]
    supported_idf_versions = [f"{version.split('.')[0]}.{version.split('.')[1]}" for version in supported_idf_versions]

    return supported_idf_versions


class IDFRelease:
    """
    ESP-IDF Release with important properties
    EOL is based on the documentation's statement of 30 months support length:
    https://docs.espressif.com/projects/esp-idf/en/latest/esp32/versions.html
    """

    def __init__(self, release_tag, published) -> None:
        self.release_tag = release_tag
        self.published = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").date()
        self.eol = self.published + relativedelta(months=+30)


def get_github_idf_releases(supported_idf_versions):
    """
    Fetches the releases of the ESP-IDF from the GitHub API.
    Returns a list of IDFRelease classes according to supported ESP-IDF versions.
    """
    url = "https://api.github.com/repos/espressif/esp-idf/releases?per_page=100"
    try:
        response = requests.get(url)
        response.raise_for_status()
        releases_raw = response.json()

    except requests.RequestException as exc:
        raise SystemExit(f"Failed to fetch ESP-IDF releases from GitHub.\nError: {exc}")

    releases = []
    for release in releases_raw:
        tag_name = release["tag_name"]
        if tag_name in supported_idf_versions:
            releases.append(IDFRelease(tag_name, release["published_at"]))

    print_color("Supported ESP-IDF Versions:")
    for version in supported_idf_versions:
        for release in releases:
            if release.release_tag == version:
                print_color(f" - {version} ... Published: {release.published} ... EOL: {release.eol}")
                json_data["supported_idf"].append(version)
    return releases


class PythonRelease:
    """
    Python release with important properties
    """

    def __init__(self, version, eol_date, is_eol) -> None:
        self.version = version
        self.eol_date = datetime.strptime(eol_date, "%Y-%m-%d").date()
        self.is_eol = is_eol


def get_supported_python_versions():
    url = "https://endoflife.date/api/v1/products/python/"
    try:
        response = requests.get(url)
        response.raise_for_status()
        releases_raw = response.json()
        releases_raw = releases_raw["result"]["releases"]

        releases = []
        for release in releases_raw:
            releases.append(PythonRelease(release["name"], release["eolFrom"], release["isEol"]))
        releases = [release for release in releases if release.eol_date > oldest_idf.published]
        return releases

    except requests.RequestException as exc:
        raise SystemExit(f"Failed to fetch supported Python versions.\nError: {exc}")


supported_idf_versions = get_supported_idf_versions()
github_releases = get_github_idf_releases(supported_idf_versions)
oldest_idf: IDFRelease = min(github_releases, key=lambda rel: rel.published)
json_data["oldest_supported_idf"] = oldest_idf.release_tag
print_color("Oldest supported ESP-IDF release:")
print_color(f" - {oldest_idf.release_tag} (Published on: {oldest_idf.published} ... EOL: {oldest_idf.eol})")

python_releases = get_supported_python_versions()
oldest_python_release = min(python_releases, key=lambda rel: rel.eol_date)
json_data["oldest_supported_python"] = oldest_python_release.version

print_color("upported Python Versions based on ESP-IDF:")
for item in python_releases:
    print_color(f" - Python {item.version}\t... EOL: {item.eol_date} ... EOL Status: {item.is_eol}")
    json_data["supported_python"].append(item.version)  # type: ignore

print_color("Oldest supported Python version:")
print_color(f" - {oldest_python_release.version} (EOL: {oldest_python_release.eol_date})")

with open("supported_versions.json", "w") as f:
    json.dump(json_data, f, indent=4)
