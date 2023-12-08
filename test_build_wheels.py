# ruff: noqa: E501
# line too long skip in ruff for whole file (formatting would be worst than long lines)
import unittest

from packaging.requirements import Requirement

from build_wheels import _change_specifier_logic
from build_wheels import yaml_to_requirement


class TestYAMLtoRequirement(unittest.TestCase):

    def test_change_specifier_logic(self):
        version_with_specifier = (('>0.9.0.2', '<0.9.0.2'),
                                  ('<0.9.0.2', '>0.9.0.2'),
                                  ('==0.9.0.2', '!=0.9.0.2'),
                                  ('>=0.9.0.2', '<=0.9.0.2'),
                                  ('<=0.9.0.2', '>=0.9.0.2'),
                                  ('!=0.9.0.2', '==0.9.0.2'),
                                  ('===0.9.0.2', '===0.9.0.2'),
                                )

        for case in version_with_specifier:
            self.assertEqual(f'{_change_specifier_logic(case[0])[0]}{_change_specifier_logic(case[0])[1]}', case[1])

    def test_yaml_to_requirement(self):
        test_requirements = {Requirement("platform;sys_platform == 'win32'"),
                             Requirement("platform;sys_platform == 'win32' or sys_platform == 'linux'"),
                             Requirement('version<42'),
                             Requirement('version<42,>50'),
                             Requirement("python;python_version > '3.10'"),
                             Requirement("python;python_version > '3.10' and python_version != '3.8'"),
                             Requirement("version-platform<=0.9.0.2;sys_platform == 'win32'"),
                             Requirement("version-platform<=0.9.0.2,>0.9.1;sys_platform == 'win32'"),
                             Requirement("version-platform<=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux'"),
                             Requirement("version-platform<=0.9.0.2,>0.9.1;sys_platform == 'win32' or sys_platform == 'linux'"),
                             Requirement("version-python<=0.9.0.2;python_version < '3.8'"),
                             Requirement("version-python<=0.9.0.2,>0.9.1;python_version < '3.8'"),
                             Requirement("version-python<=0.9.0.2;python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-python<=0.9.0.2,>0.9.1;python_version < '3.8' and python_version > '3.11'"),
                             Requirement("platform-python;sys_platform == 'win32' and python_version < '3.8'"),
                             Requirement("platform-python;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8'"),
                             Requirement("platform-python;sys_platform == 'win32' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("platform-python;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python<=0.9.0.2;sys_platform == 'win32' and python_version < '3.8'"),
                             Requirement("version-platform-python<=0.9.0.2,>0.9.1;sys_platform == 'win32' and python_version < '3.8'"),
                             Requirement("version-platform-python<=0.9.0.2,>0.9.1;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8'"),
                             Requirement("version-platform-python<=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8'"),
                             Requirement("version-platform-python<=0.9.0.2;sys_platform == 'win32' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python<=0.9.0.2,>0.9.1;sys_platform == 'win32' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python<=0.9.0.2,>0.9.1;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python<=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8' and python_version > '3.11'"),
        }

        self.assertEqual(yaml_to_requirement('test/test_list.yaml'), test_requirements)


    def test_yaml_to_requirement_exclude(self):
        test_requirements_exclude = {Requirement("platform;sys_platform != 'win32'"),
                             Requirement("platform;sys_platform != 'win32' or sys_platform != 'linux'"),
                             Requirement('version>42'),
                             Requirement('version>42,<50'),
                             Requirement("python;python_version < '3.10'"),
                             Requirement("python;python_version < '3.10' and python_version == '3.8'"),
                             Requirement("version-platform>=0.9.0.2;sys_platform == 'win32'"),
                             Requirement("version-platform;sys_platform != 'win32'"),
                             Requirement("version-platform>=0.9.0.2,<0.9.1;sys_platform == 'win32'"),
                             Requirement("version-platform;sys_platform != 'win32'"),
                             Requirement("version-platform>=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux'"),
                             Requirement("version-platform;sys_platform != 'win32' or sys_platform != 'linux'"),
                             Requirement("version-platform>=0.9.0.2,<0.9.1;sys_platform == 'win32' or sys_platform == 'linux'"),
                             Requirement("version-platform;sys_platform != 'win32' or sys_platform != 'linux'"),
                             Requirement("version-python>=0.9.0.2;python_version < '3.8'"),
                             Requirement("version-python;python_version > '3.8'"),
                             Requirement("version-python>=0.9.0.2,<0.9.1;python_version < '3.8'"),
                             Requirement("version-python;python_version > '3.8'"),
                             Requirement("version-python>=0.9.0.2;python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-python;python_version > '3.8' and python_version < '3.11'"),
                             Requirement("version-python>=0.9.0.2,<0.9.1;python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-python;python_version > '3.8' and python_version < '3.11'"),
                             Requirement("platform-python;sys_platform != 'win32' and python_version > '3.8'"),
                             Requirement("platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8'"),
                             Requirement("platform-python;sys_platform != 'win32' and python_version > '3.8' and python_version < '3.11'"),
                             Requirement("platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8' and python_version < '3.11'"),
                             Requirement("version-platform-python>=0.9.0.2;sys_platform == 'win32' and python_version < '3.8'"),
                             Requirement("version-platform-python;sys_platform != 'win32' and python_version > '3.8'"),
                             Requirement("version-platform-python>=0.9.0.2,<0.9.1;sys_platform == 'win32' and python_version < '3.8'"),
                             Requirement("version-platform-python;sys_platform != 'win32' and python_version > '3.8'"),
                             Requirement("version-platform-python>=0.9.0.2,<0.9.1;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8'"),
                             Requirement("version-platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8'"),
                             Requirement("version-platform-python>=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8'"),
                             Requirement("version-platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8'"),
                             Requirement("version-platform-python>=0.9.0.2;sys_platform == 'win32' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python;sys_platform != 'win32' and python_version > '3.8' and python_version < '3.11'"),
                             Requirement("version-platform-python>=0.9.0.2,<0.9.1;sys_platform == 'win32' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python;sys_platform != 'win32' and python_version > '3.8' and python_version < '3.11'"),
                             Requirement("version-platform-python>=0.9.0.2,<0.9.1;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8' and python_version < '3.11'"),
                             Requirement("version-platform-python>=0.9.0.2;sys_platform == 'win32' or sys_platform == 'linux' and python_version < '3.8' and python_version > '3.11'"),
                             Requirement("version-platform-python;sys_platform != 'win32' or sys_platform != 'linux' and python_version > '3.8' and python_version < '3.11'"),
        }

        self.assertEqual(yaml_to_requirement('test/test_list.yaml', exclude=True), test_requirements_exclude)


if __name__ == '__main__':
    unittest.main()
