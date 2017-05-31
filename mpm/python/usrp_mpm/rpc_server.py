#
# Copyright 2017 Ettus Research (National Instruments)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Implemented RPC Servers
"""
from __future__ import print_function
from random import choice
from string import ascii_letters, digits
from multiprocessing import Process
from gevent.server import StreamServer
from gevent.pool import Pool
from gevent import signal
from gevent import spawn_later
from gevent import Greenlet
from gevent import monkey
monkey.patch_all()
from mprpc import RPCServer
from .mpmlog import get_main_logger

TOKEN_LEN = 16 # Length of the token string

def no_claim(func):
    " Decorator for functions that require no token check "
    func._notok = True
    return func

def no_rpc(func):
    " Decorator for functions that should not be exposed via RPC "
    func._norpc = True
    return func

class MPMServer(RPCServer):
    """
    Main MPM RPC class which holds the periph_manager object and translates
    RPC calls to appropiate calls in the periph_manager and dboard_managers.

    Claiming and unclaiming is implemented in python only
    """
    _db_methods = []
    _mb_methods = []

    def __init__(self, state, mgr, *args, **kwargs):
        self.log = get_main_logger().getChild('RPCServer')
        self._state = state
        self._timer = Greenlet()
        self.periph_manager = mgr
        # add public mboard methods without namespace
        self._update_component_commands(mgr, '', '_mb_methods')
        # add public dboard methods in `db_<slot>_` namespace
        for db_slot, dboard in enumerate(mgr.dboards):
            self._update_component_commands(dboard, 'db_' + str(db_slot) + '_', '_db_methods')
        super(MPMServer, self).__init__(*args, **kwargs)

    def _update_component_commands(self, component, namespace, storage):
        """
        Detect available methods for an object and add them to the RPC server.

        We skip all private methods, and all methods that use the @no_rpc
        decorator.
        """
        for method_name in (
                m for m in dir(component)
                if not m.startswith('_') \
                    and callable(getattr(component, m)) \
                    and not getattr(getattr(component, m), '_norpc', False)
            ):
            new_rpc_method = getattr(component, method_name)
            command_name = namespace + method_name
            if getattr(new_rpc_method, '_notok', False):
                self._add_safe_command(new_rpc_method, command_name)
            else:
                self._add_claimed_command(new_rpc_method, command_name)
            getattr(self, storage).append(command_name)


    def _add_claimed_command(self, function, command):
        """
        Adds a method with the name command to the RPC server
        This command will require an acquired claim on the device, and a valid
        token needs to be passed in for it to not fail.

        If the method does not require a token, use _add_safe_command().
        """
        self.log.trace("adding command %s pointing to %s", command, function)
        def new_claimed_function(token, *args):
            " Define a function that requires a claim token check "
            if token[:TOKEN_LEN] != self._state.claim_token.value:
                return False
            return function(*args)
        new_claimed_function.__doc__ = function.__doc__
        setattr(self, command, new_claimed_function)

    def _add_safe_command(self, function, command):
        """
        Add a safe method which does not require a claim on the device.
        If the method should only be called by claimers, use
        _add_claimed_command().
        """
        self.log.trace("adding safe command %s pointing to %s", command, function)
        setattr(self, command, function)

    def list_methods(self):
        """
        Returns a tuple of public methods and
        corresponding docs of this RPC server
        """
        return [(met, getattr(self, met).__doc__)
                for met in dir(self)
                if not met.startswith('_') and callable(getattr(self, met))]

    def ping(self, data=None):
        """
        Take in data as argument and send it back
        This is a safe method which can be called without a claim on the device
        """
        self.log.debug("I was pinged from: %s:%s", self.client_host, self.client_port)
        return data

    def claim(self, sender_id):
        """
        claim `token` - tries to claim MPM device and provides a human readable sender_id
        This is a safe method which can be called without a claim on the device
        """
        self._state.lock.acquire()
        if self._state.claim_status.value:
            return ""
        self.log.debug("claiming from: %s", self.client_host)
        self.periph_manager.claimed = True
        self._state.claim_token.value = ''.join(
            choice(ascii_letters + digits) for _ in range(TOKEN_LEN)
        )
        self._state.claim_status.value = True
        self._state.lock.release()
        self.sender_id = sender_id
        self._reset_timer()
        self.log.debug("giving token: %s to host: %s", self._state.claim_token.value, self.client_host)
        return self._state.claim_token.value

    def reclaim(self, token):
        """
        reclaim a MPM device with a token. This operation will fail
        if the device is claimed and the token doesn't match.
        Or if the device is not claimed at all.
        """
        self._state.lock.acquire()
        if self._state.claim_status.value:
            if self._state.claim_token.value == token[:TOKEN_LEN]:
                self._state.lock.release()
                self.log.debug("reclaimed from: %s", self.client_host)
                self._reset_timer()
                return True
            self._state.lock.release()
            self.log.debug("reclaim failed from: %s", self.client_host)
            return False
        self.log.debug("trying to reclaim unclaimed device from: %s", self.client_host)
        return False

    def _unclaim(self):
        """
        unconditional unclaim - for internal use
        """
        self.log.debug("releasing claim")
        self._state.claim_status.value = False
        self._state.claim_token.value = ""
        self.sender_id = None
        self.periph_manager.claimed = False
        self.periph_manager.deinit()
        self._timer.kill()

    def _reset_timer(self):
        """
        reset unclaim timer
        """
        self._timer.kill()
        self._timer = spawn_later(2.0, self._unclaim)

    def unclaim(self, token):
        """
        unclaim `token` - unclaims the MPM device if it is claimed with this token
        """
        if self._state.claim_status.value and self._state.claim_token.value == token:
            self._unclaim()
            return True
        return False

    def get_device_info(self):
        """
        get device information
        This is as safe method which can be called without a claim on the device
        """
        info = self.periph_manager._get_device_info()
        if self.client_host in ["127.0.0.1", "::1"]:
            info["connection"] = "local"
        else:
            info["connection"] = "remote"
        return info

    def allocate_sid(self, token, *args):
        """
        Forwards the call to periph_manager._allocate_sid with the client ip addresss
        as argument. Should be used to setup interfaces
        """
        return self.periph_manager._allocate_sid(self.client_host, *args)




def _rpc_server_process(shared_state, port, mgr):
    """
    Start the RPC server
    """
    connections = Pool(1000)
    server = StreamServer(
        ('0.0.0.0', port),
        handle=MPMServer(shared_state, mgr),
        spawn=connections)
    # catch signals and stop the stream server
    signal(signal.SIGTERM, lambda *args: server.stop())
    signal(signal.SIGINT, lambda *args: server.stop())
    server.serve_forever()


def spawn_rpc_process(state, udp_port, mgr):
    """
    Returns a process that contains the RPC server
    """

    proc_args = [udp_port, state, mgr]
    proc = Process(target=_rpc_server_process, args=proc_args)
    proc.start()
    return proc