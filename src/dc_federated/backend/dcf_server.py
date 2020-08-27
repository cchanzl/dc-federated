"""
Defines the core server class for the federated learning.
Abstracts away the lower level server logic from the federated
machine learning logic.
"""
import json
import logging
import pickle
import hashlib
import time
import os.path
import zlib

import bottle
from bottle import request, Bottle, run, auth_basic
from dc_federated.backend._constants import *
from dc_federated.utils import get_host_ip

from nacl.signing import VerifyKey
from nacl.encoding import HexEncoder
from nacl.exceptions import BadSignatureError
from bottle import Bottle, run, request, response, ServerAdapter


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)


class DCFServer(object):
    """
    This class abstracts away the lower level communication logic for
    the central server/node from the actual federated learning logic.
    It interacts with the central server node via the 4 callback functions
    passed in the constructor. For an example usage please refer to the
    package dc_federated.example_dcf+model.

    Parameters
    ----------

        register_worker_callback:
            This function is expected to take the id of a newly registered
            worker and should contain the application specific logic for
            dealing with a new worker joining the federated learning pool.

        unregister_worker_callback:
            This function is expected to take the id of a newly unregistered
            worker and should contain the application specific logic for
            dealing with a worker leaving the federated learning pool.

        return_global_model_callback: () -> bit-string
            This function is expected to return the current global model
            in some application dependent binary serialized form.


        query_global_model_status_callback:  () -> str
            This function is expected to return a string giving the
            application dependent current status of the global model.

        receive_worker_update_callback: dict -> bool
            This function should receive a worker-id and an application
            dependent binary serialized update from the worker. The
            server code ensures that the worker-id was previously
            registered.

        key_list_file: str
            The name of the file containing the public keys for valid workers.
            The public keys are given one key per line, with each key being
            generated by the worker_key_pair_tool.py tool. If None, then
            no authentication is performed.

        server_host_ip: str (default None)
            The ip-address of the host of the server. If None, then it
            uses the ip-address of the current machine.

        server_port: int (default 8080)
            The port at which the serer should listen to. If None, then it
            uses the port 8080.

        ssl_enabled: bool (default False)
            Enable SSL/TLS for server/workers communications.

        ssl_keyfile: str
            Must be a valid path to the key file.
            This is mandatory if ssl_enabled, ignored otherwise.

        ssl_certfile: str
            Must be a valid path to the certificate.
            This is mandatory if ssl_enabled, ignored otherwise.
    """

    def __init__(
        self,
        register_worker_callback,
        unregister_worker_callback,
        return_global_model_callback,
        query_global_model_status_callback,
        receive_worker_update_callback,
        key_list_file,
        server_host_ip=None,
        server_port=8080,
        ssl_enabled=False,
        ssl_keyfile=None,
        ssl_certfile=None,
            debug=False):

        self.server_host_ip = get_host_ip() if server_host_ip is None else server_host_ip
        self.server_port = server_port

        self.register_worker_callback = register_worker_callback
        self.unregister_worker_callback = unregister_worker_callback
        self.return_global_model_callback = return_global_model_callback
        self.query_global_model_status_callback = query_global_model_status_callback
        self.receive_worker_update_callback = receive_worker_update_callback
        self.worker_authenticator = WorkerAuthenticator(key_list_file)

        self.debug = debug

        self.worker_list = []
        self.active_workers = set()
        self.last_worker = -1
        self.ssl_enabled = ssl_enabled

        if ssl_enabled:
            if ssl_certfile is None or ssl_keyfile is None:
                raise RuntimeError(
                    "When ssl is enabled, both a certfile and keyfile must be provided")
            if not os.path.isfile(ssl_certfile):
                raise IOError(
                    "The provided SSL certificate file doesn't exist")
            if not os.path.isfile(ssl_keyfile):
                raise IOError("The provided SSL key file doesn't exist")
            self.ssl_keyfile = ssl_keyfile
            self.ssl_certfile = ssl_certfile

    def is_admin(self, username, password):
        adm_username = os.environ.get('ADMIN_USERNAME')
        adm_password = os.environ.get('ADMIN_PASSWORD')

        if adm_username is None or adm_password is None:
            return False

        return username == adm_username and password == adm_password

    def register_worker(self):
        """
        Authenticates the worker

        Returns
        -------

        int:
            The id of the new client.
        """
        worker_data = request.json

        auth_success, auth_type = \
            self.worker_authenticator.authenticate_worker(worker_data[PUBLIC_KEY_STR],
                                                          worker_data[SIGNED_PHRASE])
        if not auth_success:
            logger.info(
                f"Failed to register worker with public key: {worker_data[PUBLIC_KEY_STR]}")
            return INVALID_WORKER

        logger.info(
            f"Successfully authenticated worker with public key: {worker_data[PUBLIC_KEY_STR]}")

        if auth_type == NO_AUTHENTICATION:
            worker_id = hashlib.sha224(str(time.time()).encode(
                'utf-8')).hexdigest() + '_unauthenticated'
            logger.info(
                f"Successfully registered worker: {worker_id}")

            if worker_id not in self.worker_list:
                self.worker_list.append(worker_id)

        else:
            worker_id = worker_data[PUBLIC_KEY_STR]
            if worker_id not in self.worker_list:
                logger.info(
                    f"Unauthorized worker {worker_id} tried to register")
                return INVALID_WORKER

        self.active_workers.add(worker_id)
        self.register_worker_callback(worker_id)

        return worker_id

    def admin_list_workers(self):
        """
        List all registered workers

        Returns
        -------

        [string]:
            The id of the workers
        """
        response.content_type = 'application/json'
        return json.dumps([{"worker_id": worker_id, "active": worker_id in self.active_workers} for worker_id in self.worker_list])

    def admin_add_worker(self):
        """
        Add a new worker to the list or allowed workers

        JSON Body:
        public_key_str: string The public key associated with the worker

        Returns
        -------

        The new worker id
        """
        response.content_type = 'application/json'

        worker_data = request.json

        logger.info("Admin is adding a new worker...")

        if not PUBLIC_KEY_STR in worker_data:
            logger.error(f"Public key was not not passed in {worker_data}")
            return json.dumps({
                "error": "Public key was not not passed in input"
            })

        worker_id = worker_data[PUBLIC_KEY_STR]
        logger.info(f"Worker id is {worker_id}")

        if not isinstance(worker_id, str) or not len(worker_id):
            logger.error(f"Public key should be a string: {worker_id}")
            return json.dumps({
                "error": "Public key must be a string"
            })

        if worker_id in self.worker_list:
            logger.warn(f"Worker {worker_id} already exists")
            return json.dumps({
                "error": f"Worker {worker_id} already exists"
            })

        self.worker_list.append(worker_id)
        logger.info(f"Worker {worker_id} was added")

        if "active" in worker_data and worker_data["active"] == True:
            self.active_workers.add(worker_id)
            self.register_worker_callback(worker_id)
            logger.info(f"Worker {worker_id} was registered")

        return json.dumps({
            "worker_id": worker_id,
            "active": worker_id in self.active_workers
        })

    def admin_delete_worker(self, worker_id):
        """
        Allow admin to delete a worker given its id
        """
        logger.info(f"Admin is removing worker {worker_id}...")

        if worker_id in self.worker_list:
            self.worker_list.remove(worker_id)
            # TODO callback for worker removed?
            logger.info(f"Worker {worker_id} was removed")

        if worker_id in self.active_workers:
            self.active_workers.remove(worker_id)
            self.unregister_worker_callback(worker_id)
            logger.info(f"Worker {worker_id} was unregistered (removal)")

    def admin_set_worker_status(self, worker_id):
        """
        Allow admin to change status (active = True or False) of a given worker
        """
        worker_data = request.json

        logger.info(f"Admin is setting the status of {worker_id}...")

        if not "active" in worker_data:
            logger.error(f"The status was not not passed in {worker_data}")
            return json.dumps({
                "error": f"Key 'active' is missing in payload"
            })

        active = worker_data["active"]
        logger.info(f"New {worker_id} status is active: {active}")

        if not isinstance(active, bool):
            logger.error(f"Key 'active' should be a boolean: {active}")
            return json.dumps({
                "error": f"Key 'active' should be a boolean: {active}"
            })

        if worker_id not in self.worker_list:
            logger.error(f"Unknown worker: {worker_id}")
            return json.dumps({
                "error": f"Unknown worker: {worker_id}"
            })

        prev_active = worker_id in self.active_workers
        if active and not prev_active:
            self.active_workers.add(worker_id)
            logger.info(f"Worker {worker_id} was registered")
            self.register_worker_callback(worker_id)
        elif not active and prev_active:
            self.active_workers.remove(worker_id)
            logger.info(f"Worker {worker_id} was unregistered")
            self.unregister_worker_callback(worker_id)
        else:
            logger.warn(f"Nothing to change for {worker_id}")

        return json.dumps({
            "worker_id": worker_id,
            "active": active
        })

    def receive_worker_update(self, worker_id):
        """
        This receives the update from a worker and calls the corresponding callback function.
        Expects that the worker_id and model-update were sent using the DCFWorker.send_model_update()

        Returns
        -------

        str:
            If the update was successful then "Worker update received"
            Otherwise any exception that was raised.
        """
        try:
            model_update = zlib.decompress(
                request.files[ID_AND_MODEL_KEY].file.read())

            if not worker_id in self.worker_list:
                logger.warning(
                    f"Unknown worker {worker_id} tried to send an update.")
                return UNREGISTERED_WORKER

            if not worker_id in self.active_workers:
                logger.warning(
                    f"Unregistered worker {worker_id} tried to send an update.")
                return UNREGISTERED_WORKER

            return self.receive_worker_update_callback(worker_id, model_update)

        except Exception as e:
            logger.warning(e)
            return str(e)

    def query_global_model_status(self):
        """
        Returns the status of the global model using the provided callback. If query is not
        from a valid worker it raises an error.

        Returns
        -------

        str:
            If the update was successful then "Worker update received"
            Otherwise any exception that was raised.
        """
        try:
            query_request = request.json

            if not WORKER_ID_KEY in query_request:
                logger.warning(
                    f"Key {WORKER_ID_KEY} is missing in query_request.")
                return UNREGISTERED_WORKER

            worker_id = query_request[WORKER_ID_KEY]

            if not worker_id in self.worker_list:
                logger.warning(
                    f"Unknown worker {worker_id} tried to query model status.")
                return UNREGISTERED_WORKER

            if not worker_id in self.active_workers:
                logger.warning(
                    f"Unregistered worker {worker_id} tried to query model status.")
                return UNREGISTERED_WORKER

            return self.query_global_model_status_callback()
        except Exception as e:
            logger.warning(e)
            return str(e)

    def return_global_model(self):
        """
        Returns the global model by using the provided callback. If query is not from a valid
        worker it raises an error.

        Returns
        -------

        str:
            If the update was successful then "Worker update received"
            Otherwise any exception that was raised.
        """
        try:
            query_request = request.json

            if not WORKER_ID_KEY in query_request:
                logger.warning(
                    f"Key {WORKER_ID_KEY} is missing in query_request.")
                return UNREGISTERED_WORKER

            worker_id = query_request[WORKER_ID_KEY]

            if not worker_id in self.worker_list:
                logger.warning(
                    f"Unknown worker {worker_id} tried to return global model.")
                return UNREGISTERED_WORKER

            if not worker_id in self.active_workers:
                logger.warning(
                    f"Unregistered worker {worker_id} tried to return global model.")
                return UNREGISTERED_WORKER

            return zlib.compress(self.return_global_model_callback())

        except Exception as e:
            logger.warning(e)
            return str(e)

    @staticmethod
    def enable_cors():
        """
        Enable the cross origin resource for the server.
        """
        response.add_header('Access-Control-Allow-Origin', '*')
        response.add_header('Access-Control-Allow-Methods',
                            'GET, POST, PUT, OPTIONS')
        response.add_header('Access-Control-Allow-Headers',
                            'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token')

    def start_server(self, server_adapter=None):
        """
        Sets up all the routes for the server and starts it.

        server_backend: bottle.ServerAdapter (default None)
            The server adapter to use. The default bottle.WSGIRefServer is used if none is given.
            WARNING: If given, this will over-ride the host-ip and port passed as parameters to this
            object.
        """
        application = Bottle()
        application.route(f"/{REGISTER_WORKER_ROUTE}",
                          method='POST', callback=self.register_worker)
        application.route(f"/{RETURN_GLOBAL_MODEL_ROUTE}",
                          method='POST', callback=self.return_global_model)
        application.route(f"/{QUERY_GLOBAL_MODEL_STATUS_ROUTE}",
                          method='POST', callback=self.query_global_model_status)
        application.route(f"/{RECEIVE_WORKER_UPDATE_ROUTE}/<worker_id>",
                          method='POST', callback=self.receive_worker_update)
        application.add_hook('after_request', self.enable_cors)

        # Admin routes
        application.get(
            "/workers", callback=auth_basic(self.is_admin)(self.admin_list_workers))
        application.post(
            "/workers", callback=auth_basic(self.is_admin)(self.admin_add_worker))
        application.delete("/workers/<worker_id>",
                           callback=auth_basic(self.is_admin)(self.admin_delete_worker))
        application.put("/workers/<worker_id>",
                        callback=auth_basic(self.is_admin)(self.admin_set_worker_status))

        if server_adapter is not None and isinstance(server_adapter, ServerAdapter):
            self.server_host_ip = server_adapter.host
            self.server_port = server_adapter.port
            run(application, server=server_adapter, debug=self.debug, quiet=True)
        elif self.ssl_enabled:
            run(application,
                host=self.server_host_ip,
                port=self.server_port,
                server='gunicorn',
                keyfile=self.ssl_keyfile,
                certfile=self.ssl_certfile,
                debug=self.debug,
                quiet=True)
        else:
            run(application, host=self.server_host_ip,
                port=self.server_port, debug=self.debug, quiet=True)


class WorkerAuthenticator(object):
    """
    Helper class for authenticating workers.

    Parameters
    ----------

    key_list_file: str
        The name of the file containing the public keys for valid workers.
        The file is a just list of the public keys, each generated by the
        worker_key_pair_tool tool. All workers are accepted if no workers
        are provided.
    """

    def __init__(self, key_list_file):
        if key_list_file is None:
            logger.warning(f"No key list file provided - "
                           f"no worker authentication will be used!!!.")
            logger.warning(f"Server is running in ****UNSAFE MODE.****")
            self.authenticate = False
            return

        with open(key_list_file, 'r') as f:
            keys = f.read().splitlines()

        # dict for efficient fetching of the public key
        self.authenticate = True
        self.keys = {key: VerifyKey(
            key.encode(), encoder=HexEncoder) for key in keys}

    def authenticate_worker(self, public_key_str, signed_message):
        """
        Authenticates a worker with the given public key against the
        given signed message.

        Parameters
        ----------

        public_key_str: str
            UFT-8 encoded version of the public key

        signed_message: str
            UTF-8 encoded signed message

        Returns
        -------

        bool:
            True if the public key matches the singed messge
            False otherwise
        """
        if not self.authenticate:
            logger.warning("Accepting worker as valid without authentication.")
            logger.warning(
                "Server was likely started without a list of valid public keys from workers.")
            return True, NO_AUTHENTICATION
        try:
            if public_key_str not in self.keys:
                return False, AUTHENTICATED
            self.keys[public_key_str].verify(
                signed_message.encode(), encoder=HexEncoder)
        except BadSignatureError:
            logger.warning(
                f"Failed to authenticate worker with public key: {public_key_str}.")
            return False, AUTHENTICATED
        else:
            logger.info(
                f"Successfully authenticated worker with public key: {public_key_str}.")
            return True, AUTHENTICATED
