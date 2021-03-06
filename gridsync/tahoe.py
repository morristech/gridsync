# -*- coding: utf-8 -*-

import errno
import json
import logging as log
import os
import re
import shutil
import signal
import sys
from collections import defaultdict
from io import BytesIO

import treq
from twisted.internet import reactor
from twisted.internet.defer import (
    Deferred, DeferredLock, gatherResults, inlineCallbacks, returnValue)
from twisted.internet.error import ConnectError, ProcessDone
from twisted.internet.protocol import ProcessProtocol
from twisted.internet.task import deferLater
from twisted.python.procutils import which
import yaml

from gridsync import pkgdir
from gridsync.config import Config
from gridsync.errors import NodedirExistsError
from gridsync.util import dehumanized_size


def is_valid_furl(furl):
    return re.match(r'^pb://[a-z2-7]+@[a-zA-Z0-9\.:,-]+:\d+/[a-z2-7]+$', furl)


def get_nodedirs(basedir):
    nodedirs = []
    try:
        for filename in os.listdir(basedir):
            filepath = os.path.join(basedir, filename)
            confpath = os.path.join(filepath, 'tahoe.cfg')
            if os.path.isdir(filepath) and os.path.isfile(confpath):
                log.debug("Found nodedir: %s", filepath)
                nodedirs.append(filepath)
    except OSError:
        pass
    return sorted(nodedirs)


class TahoeError(Exception):
    pass


class TahoeCommandError(TahoeError):
    pass


class TahoeWebError(TahoeError):
    pass


class CommandProtocol(ProcessProtocol):
    def __init__(self, parent, callback_trigger=None):
        self.parent = parent
        self.trigger = callback_trigger
        self.done = Deferred()
        self.output = BytesIO()

    def outReceived(self, data):
        self.output.write(data)
        data = data.decode('utf-8')
        for line in data.strip().split('\n'):
            if line:
                self.parent.line_received(line)
            if not self.done.called and self.trigger and self.trigger in line:
                self.done.callback(self.transport.pid)

    def errReceived(self, data):
        self.outReceived(data)

    def processEnded(self, reason):
        if not self.done.called:
            self.done.callback(self.output.getvalue().decode('utf-8'))

    def processExited(self, reason):
        if not self.done.called and not isinstance(reason.value, ProcessDone):
            self.done.errback(
                TahoeCommandError(
                    self.output.getvalue().decode('utf-8').strip()))


class Tahoe(object):  # pylint: disable=too-many-public-methods
    def __init__(self, nodedir=None, executable=None):
        self.executable = executable
        if nodedir:
            self.nodedir = os.path.expanduser(nodedir)
        else:
            self.nodedir = os.path.join(os.path.expanduser('~'), '.tahoe')
        self.rootcap_path = os.path.join(self.nodedir, 'private', 'rootcap')
        self.config = Config(os.path.join(self.nodedir, 'tahoe.cfg'))
        self.pidfile = os.path.join(self.nodedir, 'twistd.pid')
        self.nodeurl = None
        self.shares_happy = None
        self.name = os.path.basename(self.nodedir)
        self.api_token = None
        self.magic_folders_dir = os.path.join(self.nodedir, 'magic-folders')
        self.lock = DeferredLock()
        self.rootcap = None
        self.magic_folders = defaultdict(dict)

    def config_set(self, section, option, value):
        self.config.set(section, option, value)

    def config_get(self, section, option):
        return self.config.get(section, option)

    def get_settings(self):
        settings = {
            'nickname': self.name,
            'introducer': self.config_get('client', 'introducer.furl'),
            'shares-needed': self.config_get('client', 'shares.needed'),
            'shares-happy': self.config_get('client', 'shares.happy'),
            'shares-total': self.config_get('client', 'shares.total')
        }
        icon_path = os.path.join(self.nodedir, 'icon')
        icon_url_path = icon_path + '.url'
        if os.path.exists(icon_url_path):
            with open(icon_url_path) as f:
                settings['icon_url'] = f.read().strip()
        if os.path.exists(self.rootcap_path):
            settings['rootcap'] = self.read_cap_from_file(self.rootcap_path)
        # TODO: Verify integrity? Support 'icon_base64'?
        return settings

    def export(self, dest):
        log.debug("Exporting settings to '%s'...", dest)
        settings = self.get_settings()
        with open(dest, 'w') as f:
            f.write(json.dumps(settings))
        log.debug("Exported settings to '%s'", dest)

    def get_aliases(self):
        aliases = {}
        aliases_file = os.path.join(self.nodedir, 'private', 'aliases')
        try:
            with open(aliases_file) as f:
                for line in f.readlines():
                    if not line.startswith('#'):
                        try:
                            name, cap = line.split(':', 1)
                            aliases[name + ':'] = cap.strip()
                        except ValueError:
                            pass
            return aliases
        except IOError:
            return

    def get_alias(self, alias):
        if not alias.endswith(':'):
            alias = alias + ':'
        try:
            for name, cap in self.get_aliases().items():
                if name == alias:
                    return cap
        except AttributeError:
            return

    def load_magic_folders(self):
        data = None
        yaml_path = os.path.join(self.nodedir, 'private', 'magic_folders.yaml')
        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
        except OSError:
            pass
        if data:
            for key, value in data.items():  # to preserve defaultdict
                self.magic_folders[key] = value
        for nodedir in get_nodedirs(self.magic_folders_dir):
            folder_name = os.path.basename(nodedir)
            if folder_name not in self.magic_folders:
                config = Config(os.path.join(nodedir, 'tahoe.cfg'))
                self.magic_folders[folder_name] = {
                    'nodedir': nodedir,
                    'directory': config.get('magic_folder', 'local.directory')
                }
        return self.magic_folders

    def line_received(self, line):
        # TODO: Connect to Core via Qt signals/slots?
        log.debug("[%s] >>> %s", self.name, line)

    def _win32_popen(self, args, env, callback_trigger=None):
        # This is a workaround to prevent Command Prompt windows from opening
        # when spawning tahoe processes from the GUI on Windows, as Twisted's
        # reactor.spawnProcess() API does not allow Windows creation flags to
        # be passed to subprocesses. By passing 0x08000000 (CREATE_NO_WINDOW),
        # the opening of the Command Prompt window will be surpressed while
        # still allowing access to stdout/stderr. See:
        # https://twistedmatrix.com/pipermail/twisted-python/2007-February/014733.html
        import subprocess
        proc = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, creationflags=0x08000000)
        output = BytesIO()
        for line in iter(proc.stdout.readline, ''):
            output.write(line.encode('utf-8'))
            self.line_received(line.rstrip())
            if callback_trigger and callback_trigger in line.rstrip():
                return proc.pid
        proc.poll()
        if proc.returncode:
            raise TahoeCommandError(str(output.getvalue()).strip())
        else:
            return str(output.getvalue()).strip()

    @inlineCallbacks
    def command(self, args, callback_trigger=None):
        exe = (self.executable if self.executable else which('tahoe')[0])
        args = [exe] + ['-d', self.nodedir] + args
        env = os.environ
        env['PYTHONUNBUFFERED'] = '1'
        log.debug("Executing: %s", ' '.join(args))
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            from twisted.internet.threads import deferToThread
            output = yield deferToThread(
                self._win32_popen, args, env, callback_trigger)
        else:
            protocol = CommandProtocol(self, callback_trigger)
            reactor.spawnProcess(protocol, exe, args=args, env=env)
            output = yield protocol.done
        returnValue(output)

    @inlineCallbacks
    def version(self):
        output = yield self.command(['--version'])
        returnValue((self.executable, output.split()[1]))

    @inlineCallbacks
    def create_client(self, **kwargs):
        if os.path.exists(self.nodedir):
            raise NodedirExistsError
        valid_kwargs = ('nickname', 'introducer', 'shares-needed',
                        'shares-happy', 'shares-total')
        args = ['create-client', '--webport=tcp:0:interface=127.0.0.1']
        for key, value in kwargs.items():
            if key in valid_kwargs:
                args.extend(['--{}'.format(key), str(value)])
            elif key in ['needed', 'happy', 'total']:
                args.extend(['--shares-{}'.format(key), str(value)])
        yield self.command(args)

    @inlineCallbacks
    def _stop_magic_folder_subclients(self):
        # For magic-folders created by '_create_magic_folder_subclient' below;
        # provides support for multiple magic-folders on older tahoe clients
        tasks = []
        for nodedir in get_nodedirs(self.magic_folders_dir):
            tasks.append(Tahoe(nodedir, executable=self.executable).stop())
        yield gatherResults(tasks)

    @inlineCallbacks
    def stop(self):
        if not os.path.isfile(self.pidfile):
            log.error('No "twistd.pid" file found in %s', self.nodedir)
            return
        elif sys.platform == 'win32':
            with open(self.pidfile, 'r') as f:
                pid = f.read()
            pid = int(pid)
            log.debug("Trying to kill PID %d...", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as err:
                if err.errno not in (errno.ESRCH, errno.EINVAL):
                    log.error(err)
            os.remove(self.pidfile)
        else:
            try:
                yield self.command(['stop'])
            except TahoeCommandError:  # Process already dead/not running
                pass
        yield self._stop_magic_folder_subclients()

    @inlineCallbacks
    def _start_magic_folder_subclients(self):
        # For magic-folders created by '_create_magic_folder_subclient' below;
        # provides support for multiple magic-folders on older tahoe clients
        tasks = []
        for folder, settings in self.magic_folders.items():
            nodedir = settings.get('nodedir')
            if nodedir:
                client = Tahoe(nodedir, executable=self.executable)
                self.magic_folders[folder]['client'] = client
                tasks.append(client.start())
        yield gatherResults(tasks)

    @inlineCallbacks
    def start(self):
        if os.path.isfile(self.pidfile):
            yield self.stop()
        pid = yield self.command(['run'], 'client running')
        pid = str(pid)
        if sys.platform == 'win32' and pid.isdigit():
            with open(self.pidfile, 'w') as f:
                f.write(pid)
        with open(os.path.join(self.nodedir, 'node.url')) as f:
            self.nodeurl = f.read().strip()
        token_file = os.path.join(self.nodedir, 'private', 'api_auth_token')
        with open(token_file) as f:
            self.api_token = f.read().strip()
        self.shares_happy = int(self.config_get('client', 'shares.happy'))
        self.load_magic_folders()
        yield self._start_magic_folder_subclients()

    @staticmethod
    def _parse_welcome_page(html):
        # XXX: This can be removed once a new, stable version of
        # Tahoe-LAFS is released with Trac ticket #2476 resolved.
        # See: https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2476
        match = re.search('Connected to <span>(.+?)</span>', html)
        servers_connected = (int(match.group(1)) if match else 0)
        match = re.search("of <span>(.+?)</span> known storage servers", html)
        servers_known = (int(match.group(1)) if match else 0)
        available_space = 0
        for s in re.findall('"service-available-space">(.+?)</td>', html):
            try:
                size = dehumanized_size(s)
            except ValueError:  # "N/A"
                continue
            available_space += size
        return servers_connected, servers_known, available_space

    @inlineCallbacks  # noqa: max-complexity=11 XXX
    def get_grid_status(self):
        if not self.nodeurl:
            return
        try:
            resp = yield treq.get(self.nodeurl + '?t=json')  # not yet released
        except ConnectError:
            return
        if resp.code == 200:
            content = yield treq.content(resp)
            content = content.decode('utf-8')
            try:
                content = json.loads(content)
            except json.decoder.JSONDecodeError:
                # See: https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2476
                connected, known, space = self._parse_welcome_page(content)
                returnValue((connected, known, space))
            servers_connected = 0
            servers_known = 0
            available_space = 0
            if 'servers' in content:
                servers = content['servers']
                servers_known = len(servers)
                for server in servers:
                    if server['connection_status'].startswith('Connected'):
                        servers_connected += 1
                        if server['available_space']:
                            available_space += server['available_space']
            returnValue((servers_connected, servers_known, available_space))

    @inlineCallbacks
    def get_connected_servers(self):
        if not self.nodeurl:
            return
        try:
            resp = yield treq.get(self.nodeurl)
        except ConnectError:
            return
        if resp.code == 200:
            html = yield treq.content(resp)
            match = re.search(
                'Connected to <span>(.+?)</span>', html.decode('utf-8'))
            if match:
                returnValue(int(match.group(1)))

    @inlineCallbacks
    def is_ready(self):
        if not self.shares_happy:
            returnValue(False)
        connected_servers = yield self.get_connected_servers()
        if not connected_servers:
            returnValue(False)
        elif connected_servers >= self.shares_happy:
            returnValue(True)
        else:
            returnValue(False)

    @inlineCallbacks
    def await_ready(self):
        # TODO: Replace with "readiness" API?
        # https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2844
        ready = yield self.is_ready()
        while not ready:
            yield deferLater(reactor, 0.2, lambda: None)
            ready = yield self.is_ready()

    @inlineCallbacks
    def mkdir(self):
        resp = yield treq.post(self.nodeurl + 'uri', params={'t': 'mkdir'})
        if resp.code == 200:
            content = yield treq.content(resp)
            returnValue(content.decode('utf-8').strip())
        else:
            raise TahoeWebError(
                "Error creating Tahoe-LAFS directory: {}".format(resp.code))

    @inlineCallbacks
    def create_rootcap(self):
        log.debug("Creating rootcap...")
        if os.path.exists(self.rootcap_path):
            raise OSError(
                "Rootcap file already exists: {}".format(self.rootcap_path))
        self.rootcap = yield self.mkdir()
        with open(self.rootcap_path, 'w') as f:
            f.write(self.rootcap)
        log.debug("Rootcap saved to file: %s", self.rootcap_path)
        returnValue(self.rootcap)

    @inlineCallbacks
    def upload(self, local_path):
        log.debug("Uploading %s...", local_path)
        with open(local_path, 'rb') as f:
            resp = yield treq.put('{}uri'.format(self.nodeurl), f)
        if resp.code == 200:
            content = yield treq.content(resp)
            log.debug("Successfully uploaded %s", local_path)
            returnValue(content.decode('utf-8'))
        else:
            content = yield treq.content(resp)
            raise TahoeWebError(content.decode('utf-8'))

    @inlineCallbacks
    def download(self, cap, local_path):
        log.debug("Downloading %s...", local_path)
        resp = yield treq.get('{}uri/{}'.format(self.nodeurl, cap))
        if resp.code == 200:
            with open(local_path, 'wb') as f:
                yield treq.collect(resp, f.write)
            log.debug("Successfully downloaded %s", local_path)
        else:
            content = yield treq.content(resp)
            raise TahoeWebError(content.decode('utf-8'))

    @inlineCallbacks
    def link(self, dircap, childname, childcap):
        lock = yield self.lock.acquire()
        try:
            resp = yield treq.post(
                '{}uri/{}/?t=uri&name={}&uri={}'.format(
                    self.nodeurl, dircap, childname, childcap))
        finally:
            yield lock.release()
        if resp.code != 200:
            content = yield treq.content(resp)
            raise TahoeWebError(content.decode('utf-8'))

    @inlineCallbacks
    def unlink(self, dircap, childname):
        lock = yield self.lock.acquire()
        try:
            resp = yield treq.post(
                '{}uri/{}/?t=unlink&name={}'.format(
                    self.nodeurl, dircap, childname))
        finally:
            yield lock.release()
        if resp.code != 200:
            content = yield treq.content(resp)
            raise TahoeWebError(content.decode('utf-8'))

    @inlineCallbacks
    def _create_magic_folder_subclient(self, path, join_code=None):
        # Because Tahoe-LAFS doesn't (yet) support having multiple
        # magic-folders per tahoe client, create the magic-folder inside
        # a new nodedir using the current nodedir's connection settings.
        # See https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2792
        basename = os.path.basename(path)
        subclient = Tahoe(
            os.path.join(self.magic_folders_dir, basename),
            executable=self.executable)
        self.magic_folders[basename] = {
            'directory': path,
            'client': subclient
        }
        settings = {
            'nickname': self.config_get('node', 'nickname'),
            'introducer': self.config_get('client', 'introducer.furl'),
            'shares-needed': self.config_get('client', 'shares.needed'),
            'shares-happy': self.config_get('client', 'shares.happy'),
            'shares-total': self.config_get('client', 'shares.total')
        }
        yield subclient.create_client(**settings)
        yield subclient.start()
        yield subclient.await_ready()
        if join_code:  # XXX
            collective_cap, personal_cap = join_code.split('+')
            if collective_cap.startswith('URI:DIR2:'):  # is admin
                subclient.command(['add-alias', 'magic:', collective_cap])
                data = yield self.get_json(collective_cap)
                collective_cap_ro = data[1]['ro_uri']  # diminish to readcap
                join_code = "{}+{}".format(collective_cap_ro, personal_cap)
            yield subclient.command(
                ['magic-folder', 'join', join_code, path])
            yield subclient.stop()
            yield subclient.start()
            returnValue(subclient)
        yield subclient.command(
            ['magic-folder', 'create', 'magic:', 'admin', path])
        yield subclient.stop()
        yield subclient.start()

        rootcap = self.read_cap_from_file(self.rootcap_path)
        yield self.link(rootcap, basename + ' (collective)',
                        subclient.get_alias('magic'))
        yield self.link(rootcap, basename + ' (personal)',
                        subclient.get_magic_folder_dircap())

    @inlineCallbacks
    def create_magic_folder(self, path, join_code=None):
        try:
            os.makedirs(self.magic_folders_dir)
        except OSError:
            pass
        path = os.path.realpath(os.path.expanduser(path))
        try:
            os.makedirs(path)
        except OSError:
            pass
        name = os.path.basename(path)
        try:
            yield self.command(['magic-folder', 'create', '-n', name,
                                name + ':', 'admin', path])
        except TahoeCommandError as err:
            if str(err).endswith('not recognized'):
                yield self._create_magic_folder_subclient(path, join_code)
                return
        yield self.stop()
        yield self.start()

        rootcap = self.read_cap_from_file(self.rootcap_path)
        yield self.link(rootcap, name + ' (collective)',
                        self.get_alias(name))
        yield self.link(rootcap, name + ' (personal)',
                        self.get_magic_folder_dircap(name))

    def get_magic_folder_client(self, name):
        for folder, settings in self.magic_folders.items():
            if folder == name:
                return settings.get('client')

    @inlineCallbacks
    def magic_folder_invite(self, name, nickname):
        client = self.get_magic_folder_client(name)
        if client:
            code = yield client.command(
                ['magic-folder', 'invite', 'magic:', nickname])
        else:
            code = yield self.command(
                ['magic-folder', 'invite', '-n', name, name + ':', nickname])
        returnValue(code.strip())

    @inlineCallbacks
    def magic_folder_uninvite(self, name, nickname):
        client = self.get_magic_folder_client(name)
        if client:
            yield client.unlink(client.get_alias('magic'), nickname)
        else:
            yield self.unlink(self.get_alias(name), nickname)

    @inlineCallbacks
    def remove_magic_folder(self, name):
        if name in self.magic_folders:
            client = self.magic_folders[name].get('client')
            del self.magic_folders[name]
            if client:
                yield self.command(['magic-folder', 'leave'])
                yield client.stop()
                shutil.rmtree(client.nodedir, ignore_errors=True)
            else:
                yield self.command(['magic-folder', 'leave', '-n', name])

    @inlineCallbacks
    def get_magic_folder_status(self, name=None):
        nodeurl = self.nodeurl
        token = self.api_token
        if name:
            gateway = self.get_magic_folder_client(name)
            if gateway:
                nodeurl = gateway.nodeurl
                token = gateway.api_token
                data = {'token': token, 't': 'json'}
            else:
                data = {'token': token, 'name': name, 't': 'json'}
        else:
            data = {'token': token, 't': 'json'}
        if not nodeurl or not token:
            return
        try:
            resp = yield treq.post(nodeurl + 'magic_folder', data)
        except ConnectError:
            return
        if resp.code == 200:
            content = yield treq.content(resp)
            returnValue(json.loads(content.decode('utf-8')))

    @inlineCallbacks
    def get_json(self, cap):
        if not cap or not self.nodeurl:
            return
        uri = '{}uri/{}/?t=json'.format(self.nodeurl, cap)
        try:
            resp = yield treq.get(uri)
        except ConnectError:
            return
        if resp.code == 200:
            content = yield treq.content(resp)
            returnValue(json.loads(content.decode('utf-8')))

    @staticmethod
    def read_cap_from_file(filepath):
        try:
            with open(filepath) as f:
                cap = f.read().strip()
        except OSError:
            return
        return cap

    def get_rootcap(self):
        if not self.rootcap:
            self.rootcap = self.read_cap_from_file(self.rootcap_path)
        return self.rootcap

    def get_collective_dircap(self, name=None):
        if name in self.magic_folders:
            try:
                return self.magic_folders[name]['collective_dircap']
            except KeyError:
                pass
        gateway = self.get_magic_folder_client(name)
        if gateway:
            path = os.path.join(self.magic_folders_dir, name, 'private',
                                'collective_dircap')
        else:
            path = os.path.join(self.nodedir, 'private', 'collective_dircap')
            name = 'default'
        cap = self.read_cap_from_file(path)
        self.magic_folders[name]['collective_dircap'] = cap
        return cap

    def get_magic_folder_dircap(self, name=None):
        if name in self.magic_folders:
            try:
                return self.magic_folders[name]['upload_dircap']
            except KeyError:
                pass
        gateway = self.get_magic_folder_client(name)
        if gateway:
            path = os.path.join(self.magic_folders_dir, name, 'private',
                                'magic_folder_dircap')
        else:
            path = os.path.join(self.nodedir, 'private', 'magic_folder_dircap')
            name = 'default'
        cap = self.read_cap_from_file(path)
        if cap:
            self.magic_folders[name]['upload_dircap'] = cap
        return cap

    def get_magic_folder_directory(self, name=None):
        if name in self.magic_folders:
            try:
                return self.magic_folders[name]['directory']
            except KeyError:
                pass
        gateway = self.get_magic_folder_client(name)
        if gateway:
            directory = gateway.config_get('magic_folder', 'local.directory')
        else:
            directory = self.config_get('magic_folder', 'local.directory')
        self.magic_folders[name]['directory'] = directory
        return directory

    @inlineCallbacks
    def get_magic_folders_from_rootcap(self, content=None):
        if not content:
            content = yield self.get_json(self.get_rootcap())
        if content:
            folders = defaultdict(dict)
            for name, data in content[1]['children'].items():
                data_dict = data[1]
                if name.endswith(' (collective)'):
                    prefix = name.split(' (collective)')[0]
                    if 'rw_uri' in data_dict:
                        folders[prefix]['collective'] = data_dict['rw_uri']
                    else:
                        folders[prefix]['collective'] = data_dict['ro_uri']
                elif name.endswith(' (personal)'):
                    prefix = name.split(' (personal)')[0]
                    if 'rw_uri' in data_dict:
                        folders[prefix]['personal'] = data_dict['rw_uri']
                    else:
                        folders[prefix]['personal'] = data_dict['ro_uri']
            returnValue(folders)

    @inlineCallbacks
    def get_magic_folder_members(self, name=None, content=None):
        if not content:
            content = yield self.get_json(self.get_collective_dircap(name))
        if content:
            members = []
            children = content[1]['children']
            magic_folder_dircap = self.get_magic_folder_dircap(name)
            for member in children:
                readcap = children[member][1]['ro_uri']
                if magic_folder_dircap:
                    my_fingerprint = magic_folder_dircap.split(':')[-1]
                    fingerprint = readcap.split(':')[-1]
                    if fingerprint == my_fingerprint:
                        self.magic_folders[name]['member'] = member
                        members.insert(0, (member, readcap))
                    else:
                        members.append((member, readcap))
                else:
                    members.append((member, readcap))
            returnValue(members)

    @staticmethod
    def size_from_content(content):
        size = 0
        filenodes = content[1]['children']
        for filenode in filenodes:
            size += int(filenodes[filenode][1]['size'])
        return size

    @inlineCallbacks
    def get_magic_folder_size(self, name=None, content=None):
        if not content:
            content = yield self.get_json(self.get_magic_folder_dircap(name))
        if content:
            returnValue(self.size_from_content(content))

    @inlineCallbacks
    def get_magic_folder_info(self, name=None, members=None):
        total_size = 0
        sizes_dict = {}
        latest_mtime = 0
        if not members:
            members = yield self.get_magic_folder_members(name)
        if members:
            for member, dircap in reversed(members):
                sizes_dict[member] = {}
                json_data = yield self.get_json(dircap)
                children = json_data[1]['children']
                for filenode, data in children.items():
                    filepath = filenode.replace('@_', os.path.sep)
                    metadata = data[1]
                    try:
                        size = int(metadata['size'])
                    except KeyError:  # if linked manually
                        continue
                    sizes_dict[member][filepath] = size
                    total_size += size
                    try:
                        mt = int(metadata['metadata']['tahoe']['linkmotime'])
                    except KeyError:
                        continue
                    if mt > latest_mtime:
                        latest_mtime = mt
        returnValue((members, total_size, latest_mtime, sizes_dict))


@inlineCallbacks
def select_executable():
    if sys.platform == 'darwin' and getattr(sys, 'frozen', False):
        # Because magic-folder on macOS has not yet landed upstream
        returnValue(os.path.join(pkgdir, 'Tahoe-LAFS', 'tahoe'))
    executables = which('tahoe')
    if executables:
        tasks = []
        for executable in executables:
            log.debug("Found %s; getting version...", executable)
            tasks.append(Tahoe(executable=executable).version())
        results = yield gatherResults(tasks)
        for executable, version in results:
            log.debug("%s has version '%s'", executable, version)
            try:
                major = int(version.split('.')[0])
                minor = int(version.split('.')[1])
                if (major, minor) >= (1, 12):
                    returnValue(executable)
            except (IndexError, ValueError):
                log.warning("Could not parse/compare version of '%s'", version)
                if version == 'unknown':
                    # TODO: Check for multi-magic-folder support
                    returnValue(executable)
