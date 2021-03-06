"""Synchronize local directories with Tahoe-LAFS storage grids."""

import os
import sys

from gridsync import _version
from gridsync.config import Config

__author__ = 'Christopher R. Wood'
__url__ = 'https://github.com/gridsync/gridsync'
__license__ = 'GPLv3'
__version__ = _version.__version__


default_settings = {
    'application': {
        'name': 'Gridsync',
        'tray_icon': 'gridsync.png',
        'tray_icon_sync': 'sync.gif'
    },
    'default': {
        'introducer': None,
        'shares-needed': None,
        'shares-happy': None,
        'shares-total': None,
        'provider_name': None,
        'provider_icon': None
    },
    'help': {
        'docs_url': 'docs.gridsync.io',
        'issues_url': 'https://github.com/gridsync/gridsync/issues'
    },
    'wormhole': {
        'appid': 'lothar.com/wormhole/text-or-file-xfer',
        'relay': 'ws://relay.magic-wormhole.io:4000/v1'
    }
}


if getattr(sys, 'frozen', False):
    pkgdir = os.path.dirname(os.path.realpath(sys.executable))
    os.environ["PATH"] += os.pathsep + os.path.join(pkgdir, 'Tahoe-LAFS')
    try:
        del sys.modules['twisted.internet.reactor']  # PyInstaller workaround
    except KeyError:
        pass
else:
    pkgdir = os.path.dirname(os.path.realpath(__file__))


settings = Config(os.path.join(pkgdir, 'resources', 'config.txt')).load()

if not settings:
    settings = default_settings

APP_NAME = settings['application']['name']

if sys.platform == 'win32':
    config_dir = os.path.join(
        os.getenv('APPDATA'), APP_NAME)
elif sys.platform == 'darwin':
    config_dir = os.path.join(
        os.path.expanduser('~'), 'Library', 'Application Support', APP_NAME)
else:
    config_home = os.environ.get(
        'XDG_CONFIG_HOME', os.path.join(os.path.expanduser('~'), '.config'))
    config_dir = os.path.join(
        config_home, APP_NAME.lower())


def resource(filename):
    return os.path.join(pkgdir, 'resources', filename)
