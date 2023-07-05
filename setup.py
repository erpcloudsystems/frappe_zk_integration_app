from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in frappe_zk_integration_app/__init__.py
from frappe_zk_integration_app import __version__ as version

setup(
	name="frappe_zk_integration_app",
	version=version,
	description="A frappe app to help connect to fingerprint scanners via the ZK protocol",
	author="Creative Advanced Technologies",
	author_email="info@creativeadvtech.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
