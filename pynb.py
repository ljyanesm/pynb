import threading
import time
import getpass
import os
import socket
import select
import webbrowser
import uuid,hashlib
from notebook.auth import passwd
try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import sys
from optparse import OptionParser
import paramiko
SSH_PORT = 22
DEFAULT_PORT = 8888
g_verbose = True

class ForwardServer(SocketServer.ThreadingTCPServer):
    pass


class Handler(SocketServer.BaseRequestHandler):

    def handle(self):
        try:
            chan = self.ssh_transport.open_channel('direct-tcpip', (self.chain_host, self.chain_port), self.request.getpeername())
        except Exception as e:
            return

        if chan is None:
            return
        else:
            while True:
                r, w, x = select.select([self.request, chan], [], [])
                if self.request in r:
                    data = self.request.recv(1024)
                    if len(data) == 0:
                        break
                    chan.send(data)
                if chan in r:
                    data = chan.recv(1024)
                    if len(data) == 0:
                        break
                    self.request.send(data)

            peername = self.request.getpeername()
            chan.close()
            self.request.close()
            return


def forward_tunnel(local_port, remote_host, remote_port, transport):

    class SubHander(Handler):
        chain_host = remote_host
        chain_port = remote_port
        ssh_transport = transport

    server = ForwardServer(('', local_port), SubHander)
    return threading.Thread(target=server.serve_forever)


def verbose(s):
    global g_verbose
    if g_verbose:
        print s


HELP = 'Set up a forward tunnel across an SSH server, using paramiko. A local port\n(given with -p) is forwarded across an SSH session to an address:port from\nthe SSH server. This is similar to the openssh -L option.\n'

def get_host_port(spec, default_port):
    args = (spec.split(':', 1) + [default_port])[:2]
    args[1] = int(args[1])
    return (args[0], args[1])


def parse_options():
    global g_verbose
    parser = OptionParser(usage='usage: %prog [options] <ssh-server>[:<server-port>]', version='%prog 1.0', description=HELP)
    parser.add_option('-q', '--quiet', action='store_false', dest='verbose', default=True, help='squelch all informational output')
    parser.add_option('-p', '--local-port', action='store', type='int', dest='port', default=DEFAULT_PORT, help='local port to forward (default: %d)' % DEFAULT_PORT)
    parser.add_option('-u', '--user', action='store', type='string', dest='user', default=getpass.getuser(), help='username for SSH authentication (default: %s)' % getpass.getuser())
    parser.add_option('-K', '--key', action='store', type='string', dest='keyfile', default=None, help='private key file to use for SSH authentication')
    parser.add_option('', '--no-key', action='store_false', dest='look_for_keys', default=True, help="don't look for or use a private key file")
    parser.add_option('-P', '--password', action='store_true', dest='readpass', default=False, help='read password (for key or password auth) from stdin')
    parser.add_option('-r', '--remote', action='store', type='string', dest='remote', default=None, metavar='host:port', help='remote host and port to forward to')
    options, args = parser.parse_args()
    if len(args) != 1:
        parser.error('Incorrect number of arguments.')
    if options.remote is None:
        parser.error('Remote address required (-r).')
    g_verbose = options.verbose
    server_host, server_port = get_host_port(args[0], SSH_PORT)
    remote_host, remote_port = get_host_port(options.remote, SSH_PORT)
    return (options, (server_host, server_port), (remote_host, remote_port))


def main():
    options, server, remote = parse_options()
    password = None
    if options.readpass:
        password = getpass.getpass('Enter SSH password: ')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(server[0], server[1], username=options.user, key_filename=options.keyfile, look_for_keys=options.look_for_keys, password=password)
    except Exception as e:
        print '*** Failed to connect to %s:%d: %r' % (server[0], server[1], e)
        sys.exit(1)

    try:
        thread = forward_tunnel(options.port, remote[0], remote[1], client.get_transport())
        thread.daemon = True
        thread.start()
    except KeyboardInterrupt:
        print 'C-c: Port forwarding stopped.'
        sys.exit(0)
    print server
    print remote
    pwd = raw_input("Introduce a password to protect the notebook: ")
    pwd = passwd(pwd)
    client.exec_command("""
echo "#!/bin/bash" > jupyternb.sbatch
echo "#SBATCH -p tgac-short" >> jupyternb.sbatch
echo "source /tgac/software/testing/bin/lmod-6.1;" >> jupyternb.sbatch
echo "hostname;" >> jupyternb.sbatch
echo "ssh -R %s:localhost:8888 %s -f -nNT;" >> jupyternb.sbatch
echo "ml python_anaconda;" >> jupyternb.sbatch
echo "echo \\\"c.NotebookApp.password = u'%s'\\\" > .jupyter/slurm_config.py" >> jupyternb.sbatch
echo "jupyter notebook --no-browser --config=~/.jupyter/slurm_config.py" >> jupyternb.sbatch
""" % (remote[1], server[0], pwd))

    time.sleep(1)
    stdin, stdout, stderr = client.exec_command("""sbatch -p tgac-short --mem 32G -c 8 jupyternb.sbatch""")
    job_info = stdout.readlines()
    print job_info
    jobid = job_info[0].split(' ')[3]
    # Here capture the stdout of the job and check that the notebook has started, print the status to the log (queue status)
    time.sleep(10)
    webbrowser.open("http://localhost:%s" % (options.port))
    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print 'Exiting...'
        print 'Cancelling %s' % jobid
        client.exec_command('rm .jupyter/jupyter_notebook_config.py')
        client.exec_command('rm jupyternb.sbatch')
        client.exec_command('scancel %s' % jobid)
        thread.join(3)

    return


if __name__ == '__main__':
    main()

