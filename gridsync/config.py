# -*- coding: utf-8 -*-

import logging
import os
import sys
import yaml


class Config():
    def __init__(self, config_file=None):
        #if 'win' in sys.platform:
        #    self.config_dir = os.path.join(os.getenv('APPDATA'), 'Gridsync')
        if sys.platform == 'darwin':
            self.config_dir = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Gridsync')
        else:
            self.config_dir = os.path.join(os.path.expanduser('~'), '.config', 'gridsync')
        if not os.path.isdir(self.config_dir):
            os.makedirs(self.config_dir)

        if config_file:
            self.config_file = config_file[0]
        else:
            self.config_file = os.path.join(self.config_dir, 'gridsync.yml')

    def load(self):
        with open(self.config_file) as f:
            return yaml.load(f)

    def save(self, dict):
        logging.info('Saving config to {}'.format(self.config_file))
        with open(self.config_file, 'w') as f:
            try:
                os.chmod(self.config_file, 0o600)
            except:
                pass
            yaml.dump(dict, f, indent=4, default_flow_style=False)

