pymodaq_plugins_imagingsource
###########################################

.. the following must be adapted to your developed package, links to pypi, github  description...

.. image:: https://img.shields.io/pypi/v/pymodaq_plugins_imagingsource.svg
   :target: https://pypi.org/project/pymodaq_plugins_imagingsource/
   :alt: Latest Version

.. image:: https://readthedocs.org/projects/pymodaq/badge/?version=latest
   :target: https://pymodaq.readthedocs.io/en/stable/?badge=latest
   :alt: Documentation Status

.. image:: https://github.com/ccabello99/pymodaq_plugins_imagingsource/workflows/Upload%20Python%20Package/badge.svg
   :target: https://github.com/ccabello99/pymodaq_plugins_imagingsource
   :alt: Publication Status

.. image:: https://github.com/ccabello99/pymodaq_plugins_imagingsource/actions/workflows/Test.yml/badge.svg
    :target: https://github.com/ccabello99/pymodaq_plugins_imagingsource/actions/workflows/Test.yml


PyMoDAQ plugin for interfacing with Imaging Source Cameras


Authors
=======

* First Author  (christian.cabello@ip-paris.fr)


Instruments
===========

Below is the list of instruments included in this plugin

Actuators
+++++++++

Viewer0D
++++++++

Viewer1D
++++++++


Viewer2D
++++++++

* **DMK**: control of DMK Imaging Source cameras


PID Models
==========


Extensions
==========


Installation instructions
=========================
* Tested on PyMoDAQ version \>5.0.5
* Tested on Windows 10/11 and Ubuntu 24.04
* Must install appropriate drivers and SDK from https://www.theimagingsource.com/en-us/support/download/ before running
* Config files are needed for different camera models. Examples for DMK-42BUC03 and DMK-33GR0134 are given in the resources directory. The module will look for this file in the C:/ProgramData/.pymodaq folder in Windows and /etc/.pymodaq folder in Linux and if not found, a default config file can be created upon camera initialization.
