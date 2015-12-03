#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import argparse
import logging
import os
import socket
import sys

# Workarounds for PyInstaller
if getattr(sys, 'frozen', False):
    del sys.modules['twisted.internet.reactor']
    if sys.platform == 'darwin':
        bundled_tahoe_path = os.path.dirname(sys.executable) + '/Tahoe-LAFS'
        os.environ["PATH"] += os.pathsep + bundled_tahoe_path

from twisted.internet.error import CannotListenError

from gridsync import __doc__ as description
from gridsync._version import __version__
from gridsync.server import Server


def main():
    parser = argparse.ArgumentParser(
            description=description,
            epilog='Example: %(prog)s <URI>')
    parser.add_argument(
            'command',
            nargs='*',
            help='Command to send (e.g., "stop", "reload", "sync").')
    parser.add_argument(
            '-c',
            '--config',
            metavar='<file>',
            nargs=1,
            help='Load settings from config file.')
    parser.add_argument(
            '-d',
            '--node-directory',
            metavar='<file>',
            nargs=1,
            help='Specify Tahoe directory.')
    parser.add_argument(
            '--debug',
            action='store_true',
            help='Print debug messages to STDOUT.')
    parser.add_argument(
            '-g',
            '--no-gui',
            action='store_true',
            help='Run without GUI.')
    parser.add_argument(
            '-V',
            '--version',
            action="version",
            version='%(prog)s ' + __version__)
    args = parser.parse_args()

    try:
        gridsync = Server(args)
        gridsync.start()
    except CannotListenError:
        if args.command:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(("localhost", 52045))
                s.send(' '.join(args.command))
                return 0
            except Exception as e:
                logging.error(str(e))
                sys.exit(str(e), file=sys.stderr)
                return 1
        else:
            logging.error("Gridsync already running.")
            print("Gridsync already running.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
