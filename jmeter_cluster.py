#!/usr/bin/env python
# Copyright 2013 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Command line tool for JMeter server cluster on Google Compute Engine.

The script starts Google Compute Engine instances, install JMeter,
and starts JMeter servers on them.  The script can also start JMeter
client on local machine, that are connected to JMeter servers started
in advance.
"""



import argparse
import os
import os.path
import re
import subprocess
import sys
import time

import apiclient
import apiclient.errors
import oauth2client

from gce_api import GceApi


# Project-related configuration.
CLIENT_ID = '{{{{ client_id }}}}'
CLIENT_SECRET = '{{{{ client_secret }}}}'

CLOUD_STORAGE = 'gs://{{{{ cloud_storage }}}}'
DEFAULT_PROJECT = '{{{{ project_id }}}}'
DEFAULT_ZONE = 'us-central1-a'
DEFAULT_IMAGE = 'projects/debian-cloud/global/images/debian-7-wheezy-v20130515'
DEFAULT_MACHINE_TYPE = 'n1-standard-2'

GCE_STATUS_CHECK_INTERVAL = 3


class JMeterFiles(object):
  """Class to handle local files for JMeter client."""

  CLIENT_DIR = 'apache-jmeter-2.8-client'
  STARTUP_SCRIPT = ['startup.sh']
  CLIENT_CONFIG = [CLIENT_DIR, 'bin', 'jmeter.properties']
  CLIENT_JMETER = [CLIENT_DIR, 'bin', 'jmeter.sh']

  @classmethod
  def _GetPath(cls, *params):
    return os.path.join(os.path.relpath(os.path.dirname(__file__)),
                        *list(*params))

  @classmethod
  def GetStartupScriptPath(cls):
    return cls._GetPath(cls.STARTUP_SCRIPT)

  @classmethod
  def RunJmeterClient(cls, *params):
    executable = cls._GetPath(cls.CLIENT_JMETER)
    command = ' '.join([executable, '-Djava.rmi.server.hostname=127.0.0.1'] +
                       list(params))
    subprocess.call(command, shell=True)

  @classmethod
  def RewriteConfig(cls, regexp_pattern, replace_string):
    """Rewrite JMeter config file with regular expression rule.

    The function reads jmeter.properties and overwrites the same file.

    Args:
      regexp_pattern: Regular expression rule in string to match against.
      replace_string: New string to replace the rule with.
    """
    config_file = cls._GetPath(cls.CLIENT_CONFIG)

    with open(config_file) as f:
      contents = f.read()

    new_contents = re.compile(regexp_pattern, re.MULTILINE).sub(
        replace_string, contents)

    with open(config_file, 'w') as f:
      f.write(new_contents)


class JMeterCluster(object):
  """Class to manipulate JMeter server cluster on Google Compute Engine."""

  def __init__(self, params):
    self.params = params
    self.api = None

  def _GetGceApi(self):
    """Set up and get GoogleComputeEngine object if necessary."""
    if not self.api:
      self.project = (getattr(self.params, 'project', None) or DEFAULT_PROJECT)
      self.zone = (getattr(self.params, 'zone', None) or DEFAULT_ZONE)
      self.image = (getattr(self.params, 'image', None) or DEFAULT_IMAGE)
      self.machine_type = (getattr(self.params, 'machinetype', None)
                           or DEFAULT_MACHINE_TYPE)

      if not self.project:
        sys.stderr.write(
            '\nPlease specify a project using the --project option.\n\n')
        os.exit(1)

      self.api = GceApi('jmeter_cluster', CLIENT_ID, CLIENT_SECRET,
                        self.project, self.zone)
    return self.api

  def _MakeInstanceName(self, index):
    return '%s-%03d' % (self.params.prefix, index)

  def _WaitForAllInstancesRunning(self):
    size = self.params.size
    while True:
      print 'Checking instance status...'
      status_count = {}
      for index in xrange(size):
        status = self._GetGceApi().GetInstance(
            self._MakeInstanceName(index))['status']
        status_count[status] = status_count.get(status, 0) + 1
      print 'Total instances: %d' % size
      for status, count in status_count.items():
        print '  %s: %d' % (status, count)
      print
      if status_count.get('RUNNING', 0) == size:
        break
      time.sleep(GCE_STATUS_CHECK_INTERVAL)

  def Start(self):
    """Starts up JMeter server cluster."""
    size = self.params.size

    startup_script = open(JMeterFiles.GetStartupScriptPath()).read() % (
        CLOUD_STORAGE)

    for index in xrange(size):
      instance_name = self._MakeInstanceName(index)
      print 'Starting instance: %s' % instance_name
      self._GetGceApi().CreateInstance(
          instance_name, self.machine_type, self.image,
          startup_script=startup_script,
          service_accounts=[
              'https://www.googleapis.com/auth/devstorage.read_only'],
          metadata={'id': index})

    self._WaitForAllInstancesRunning()
    self.SetPortForward()

  def SetPortForward(self):
    """Sets up SSH port forwarding."""
    project = getattr(self.params, 'project', None) or DEFAULT_PROJECT

    server_list = []
    for index in xrange(self.params.size):
      instance_name = self._MakeInstanceName(index)
      print 'Setting up port forwarding for: %s' % instance_name
      server_port = 24000 + index
      server_rmi_port = 26000 + index
      client_rmi_port = 25000
      # Run "gcutil ssh" command to activate SSH port forwarding.
      command = [
          'gcutil', '--project', project, 'ssh',
          '--ssh_arg', '-L%(server_port)d:127.0.0.1:%(server_port)d',
          '--ssh_arg', '-L%(server_rmi_port)d:127.0.0.1:%(server_rmi_port)d',
          '--ssh_arg', '-R%(client_rmi_port)d:127.0.0.1:%(client_rmi_port)d',
          '--ssh_arg', '-N',
          '--ssh_arg', '-f',
          '%(instance_name)s']
      subprocess.call(
          ' '.join(command) % {
              'instance_name': instance_name,
              'server_port': server_port,
              'server_rmi_port': server_rmi_port,
              'client_rmi_port': client_rmi_port,
          },
          shell=True)
      server_list.append('127.0.0.1:%d' % server_port)

    # Update remote_hosts configuration in client configuration.
    JMeterFiles.RewriteConfig('(?<=^remote_hosts=).*',
                              ','.join(server_list))

  def ShutDown(self):
    """Shuts down JMeter server cluster."""
    name_filter = 'name eq ^%s-.*' % self.params.prefix
    while True:
      instances = self._GetGceApi().ListInstances(name_filter)
      instance_names = [i['name'] for i in instances]
      if not instance_names:
        break
      print 'Delete instances:'
      for name in instance_names:
        print '  ' + name
        self._GetGceApi().DeleteInstance(name)

      for _ in xrange(10):
        still_alive = []
        for name in instance_names:
          try:
            self._GetGceApi().GetInstance(name)
            still_alive.append(name)
          except apiclient.errors.HttpError:
            print 'Instance deleted: %s' % name
        if not still_alive:
          break
        instance_names = still_alive
        time.sleep(GCE_STATUS_CHECK_INTERVAL)


def Start(params):
  """Sub-command handler for 'start'."""
  jmeter_cluster = JMeterCluster(params)
  jmeter_cluster.Start()


def ShutDown(params):
  """Sub-command handler for 'shutdown'."""
  jmeter_cluster = JMeterCluster(params)
  jmeter_cluster.ShutDown()


def PortForward(params):
  """Sub-command handler for 'portforward'."""
  jmeter_cluster = JMeterCluster(params)
  jmeter_cluster.SetPortForward()


def Client(unused_params, *additional_args):
  """Sub-command handler for 'client'."""
  JMeterFiles.RunJmeterClient(*additional_args)


class JMeterExecuter(object):
  """Class to parse command line arguments and execute sub-commands."""

  def __init__(self):
    self.parser = argparse.ArgumentParser()

    # Specify --noauth_local_webserver as instructed when you use remote
    # terminal such as ssh.
    class SetNoAuthLocalWebserverAction(argparse.Action):
      def __call__(self, parser, namespace, values, option_string=None):
        oauth2client.tools.gflags.FLAGS.auth_local_webserver = False

    self.parser.add_argument(
        '--noauth_local_webserver', nargs=0,
        action=SetNoAuthLocalWebserverAction,
        help='Do not attempt to open browser on local machine.')

    self.subparsers = self.parser.add_subparsers(
        title='Sub-commands', dest='subcommand')

  def _AddGceWideParams(self, subparser):
    subparser.add_argument(
        '--project',
        help='Project name to start Google Compute Engine instances in.')
    subparser.add_argument(
        '--prefix', default='%s-jmeter' % os.environ['USER'],
        help='Name prefix of Google Compute Engine instances. '
        '(default "$USER-jmeter")')

  def _AddStartSubcommand(self):
    parser_start = self.subparsers.add_parser(
        'start',
        help='Start JMeter server cluster.  Also sets port forwarding.')
    parser_start.add_argument(
        'size', default=3, type=int, nargs='?',
        help='JMeter server cluster size. (default 3)')
    self._AddGceWideParams(parser_start)
    parser_start.add_argument(
        '--zone',
        help='Zone name where to add JMeter server cluster.')
    parser_start.add_argument(
        '--image',
        help='Machine image of Google Compute Engine instance.')
    parser_start.add_argument(
        '--machinetype',
        help='Machine type of Google Compute Engine instance.')
    parser_start.set_defaults(handler=Start)

  def _AddShutdownSubcommand(self):
    parser_shutdown = self.subparsers.add_parser(
        'shutdown',
        help='Tear down JMeter server cluster.')
    self._AddGceWideParams(parser_shutdown)
    parser_shutdown.set_defaults(handler=ShutDown)

  def _AddPortforwardSubcommand(self):
    parser_portforward = self.subparsers.add_parser(
        'portforward',
        help='Set up JMeter SSH port forwarding.')
    parser_portforward.add_argument(
        'size', default=3, type=int, nargs='?',
        help='JMeter server cluster size. (default 3)')
    self._AddGceWideParams(parser_portforward)
    parser_portforward.set_defaults(handler=PortForward)

  def _AddClientSubcommand(self):
    parser_client = self.subparsers.add_parser(
        'client',
        help='Start JMeter client.  Can take additional parameters passed to '
        'JMeter.')
    parser_client.set_defaults(handler=Client)

  def ParseArgumentsAndExecute(self, argv):
    """Parses command arguments and starts sub-command handler.

    Args:
      argv: Parameters in list of strings.
    """
    self._AddStartSubcommand()
    self._AddShutdownSubcommand()
    self._AddPortforwardSubcommand()
    self._AddClientSubcommand()

    # Parse command-line arguments and execute corresponding handler function.
    params, additional_args = self.parser.parse_known_args(argv)
    # Execute handler function given by "handler" parameter.
    params.handler(params, *additional_args)


def main():
  executer = JMeterExecuter()
  executer.ParseArgumentsAndExecute(sys.argv[1:])


if __name__ == '__main__':
  main()
