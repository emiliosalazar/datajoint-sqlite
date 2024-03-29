"""
This module contains the Connection class that manages the connection to the database,
 and the `conn` function that provides access to a persistent connection in datajoint.
"""
import warnings
from contextlib import contextmanager
import pymysql as client
import sqlite3
import logging
from getpass import getpass

from .settings import config
from . import errors
from .dependencies import Dependencies

from .utils import OrderedDict

#import os
#logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))

# client errors to catch
client_errors = (client.err.InterfaceError, client.err.DatabaseError)


def translate_query_error(client_error, query):
    """
    Take client error and original query and return the corresponding DataJoint exception.
    :param client_error: the exception raised by the client interface
    :param query: sql query with placeholders
    :return: an instance of the corresponding subclass of datajoint.errors.DataJointError
    """
    # Loss of connection errors
    if isinstance(client_error, client.err.InterfaceError) and client_error.args[0] == "(0, '')":
        return errors.LostConnectionError('Server connection lost due to an interface error.', *client_error.args[1:])
    disconnect_codes = {
        2006: "Connection timed out",
        2013: "Server connection lost"}
    if isinstance(client_error, client.err.OperationalError) and client_error.args[0] in disconnect_codes:
        return errors.LostConnectionError(disconnect_codes[client_error.args[0]], *client_error.args[1:])
    # Access errors
    if isinstance(client_error, client.err.OperationalError) and client_error.args[0] in (1044, 1142):
        return errors.AccessError('Insufficient privileges.', client_error.args[1],  query)
    # Integrity errors
    if isinstance(client_error, client.err.IntegrityError) and client_error.args[0] == 1062:
        return errors.DuplicateError(*client_error.args[1:])
    if isinstance(client_error, client.err.IntegrityError) and client_error.args[0] == 1452:
        return errors.IntegrityError(*client_error.args[1:])
    # Syntax errors
    if isinstance(client_error, client.err.ProgrammingError) and client_error.args[0] == 1064:
        return errors.QuerySyntaxError(client_error.args[1], query)
    # Existence errors
    if isinstance(client_error, client.err.ProgrammingError) and client_error.args[0] == 1146:
        return errors.MissingTableError(client_error.args[1], query)
    if isinstance(client_error, client.err.InternalError) and client_error.args[0] == 1364:
        return errors.MissingAttributeError(*client_error.args[1:])
    if isinstance(client_error, client.err.InternalError) and client_error.args[0] == 1054:
        return errors.UnknownAttributeError(*client_error.args[1:])
    # all the other errors are re-raised in original form
    return client_error


logger = logging.getLogger(__name__)


def conn(host=None, user=None, password=None, *, init_fun=None, reset=False, use_tls=None):
    """
    Returns a persistent connection object to be shared by multiple modules.
    If the connection is not yet established or reset=True, a new connection is set up.
    If connection information is not provided, it is taken from config which takes the
    information from dj_local_conf.json. If the password is not specified in that file
    datajoint prompts for the password.

    :param host: hostname
    :param user: mysql user
    :param password: mysql password
    :param init_fun: initialization function
    :param reset: whether the connection should be reset or not
    :param use_tls: TLS encryption option. Valid options are: True (required),
                    False (required no TLS), None (TLS prefered, default),
                    dict (Manually specify values per
                    https://dev.mysql.com/doc/refman/5.7/en/connection-options.html
                        #encrypted-connection-options).
    """
    if not hasattr(conn, 'connection') or reset:
        host = host if host is not None else config['database.host']
        user = user if user is not None else config['database.user']
        password = password if password is not None else config['database.password']
        if user is None:  # pragma: no cover
            user = input("Please enter DataJoint username: ")
        if password is None:  # pragma: no cover
            password = getpass(prompt="Please enter DataJoint password: ")
        init_fun = init_fun if init_fun is not None else config['connection.init_function']
        use_tls = use_tls if use_tls is not None else config['database.use_tls']
        conn.connection = Connection(host, user, password, None, init_fun, use_tls)
    return conn.connection


class Connection:
    """
    A dj.Connection object manages a connection to a database server.
    It also catalogues modules, schemas, tables, and their dependencies (foreign keys).

    Most of the parameters below should be set in the local configuration file.

    :param host: host name, may include port number as hostname:port, in which case it overrides the value in port
    :param user: user name
    :param password: password
    :param port: port number
    :param init_fun: connection initialization function (SQL)
    :param use_tls: TLS encryption option
    """

    def __init__(self, host, user, password, port=None, init_fun=None, use_tls=None):
        if port != 'sqlite':
            if ':' in host:
                # the port in the hostname overrides the port argument
                host, port = host.split(':')
                port = int(port)
            elif port is None:
                port = config['database.port']
        self.conn_info = dict(host=host, port=port, user=user, passwd=password)

        if use_tls is not False:
            self.conn_info['ssl'] = use_tls if isinstance(use_tls, dict) else {'ssl': {}}
        self.conn_info['ssl_input'] = use_tls
        self.init_fun = init_fun
        
        if port != 'sqlite':
            print("Connecting {user}@{host}:{port}".format(**self.conn_info))
        else:
            print("Connecting to sqlite database {host}".format(**self.conn_info))
        self._conn = None
        self.connect()

        if self.is_connected:
            if port != 'sqlite':
                logger.info("Connected {user}@{host}:{port}".format(**self.conn_info))
                self.connection_id = self.query('SELECT connection_id()').fetchone()[0]
            else:
                logger.info("Connected to sqlite database {host}".format(**self.conn_info))
        else:
            raise errors.ConnectionError('Connection failed.')

        self._in_transaction = False
        self.schemas = dict()
        self.dependencies = Dependencies(self)

    def __eq__(self, other):
        return self.conn_info == other.conn_info

    def __repr__(self):
        connected = "connected" if self.is_connected else "disconnected"
        return "DataJoint connection ({connected}) {user}@{host}:{port}".format(
            connected=connected, **self.conn_info)

    def connect(self):
        """
        Connects to the database server.
        """
        if self.conn_info['port'] != 'sqlite':
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', '.*deprecated.*')
                try:
                    self._conn = client.connect(
                        init_command=self.init_fun,
                        sql_mode="NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,"
                                 "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION",
                        charset=config['connection.charset'],
                        **{k: v for k, v in self.conn_info.items()
                           if k != 'ssl_input'})
                except client.err.InternalError:
                    self._conn = client.connect(
                        init_command=self.init_fun,
                        sql_mode="NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,"
                                 "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION",
                        charset=config['connection.charset'],
                        **{k: v for k, v in self.conn_info.items()
                           if not(k == 'ssl_input' or
                                  k == 'ssl' and self.conn_info['ssl_input'] is None)})
            self._conn.autocommit(True)
        else:
            bashCommand = "fs flush '%s'" % self.conn_info['host']
            import subprocess
            process = subprocess.Popen(bashCommand, shell=True)
            output, error = process.communicate()

            # we set isolation_level to None to avoid Python's sqlite library from overruling transaction begin/ends
            self._conn = sqlite3.connect(self.conn_info['host'], isolation_level=None)
            # enforce foreign key constraints... which apparently isn't the
            # default setting in sqlite O.o
            self.query('PRAGMA foreign_keys=1') 

    def close(self):
        self._conn.close()

    def register(self, schema):
        self.schemas[schema.database] = schema

    def ping(self):
        """
        Pings the connection. Raises an exception if the connection is closed.
        """
        self._conn.ping(reconnect=False)

    @property
    def is_connected(self):
        """
        Returns true if the object is connected to the database server.
        """
        try:
            self.ping()
        except AttributeError:
            if self.conn_info['port'] == 'sqlite':
                return True
            return False
        else:
            return False
        return True

    @staticmethod
    def _execute_query(cursor, query, args, cursor_class, suppress_warnings):
        try:
            with warnings.catch_warnings():
                if suppress_warnings:
                    # suppress all warnings arising from underlying SQL library
                    warnings.simplefilter("ignore")
                cursor.execute(query, args)
        except client_errors as err:
            raise translate_query_error(err, query) from None

    def query(self, query, args=(), *, as_dict=False, suppress_warnings=True, reconnect=None):
        """
        Execute the specified query and return the tuple generator (cursor).
        :param query: SQL query
        :param args: additional arguments for the client.cursor
        :param as_dict: If as_dict is set to True, the returned cursor objects returns
                        query results as dictionary.
        :param suppress_warnings: If True, suppress all warnings arising from underlying query library
        :param reconnect: when None, get from config, when True, attempt to reconnect if disconnected
        """
        if reconnect is None:
            reconnect = config['database.reconnect']
        logger.debug("Executing SQL:" + query[0:300])

        if self.conn_info['port'] != 'sqlite':
            cursor_class = client.cursors.DictCursor if as_dict else client.cursors.Cursor
            cursor = self._conn.cursor(cursor=cursor_class)
        else:
            cursor_class = None
            if as_dict:
                # might not be the super most-efficient, but trying to match what MySQL would do
                def dict_factory(cursor, row):
                    d = {}
                    for idx, col in enumerate(cursor.description):
                        d[col[0]] = row[idx]
                    d = OrderedDict(d) # this does nothing for Python 3.6+, buut... to maybe be backwards compatible?
                    return d                
                self._conn.row_factory = dict_factory
            else:
                self._conn.row_factory = None
            cursor = self._conn.cursor()

        try:
            self._execute_query(cursor, query, args, cursor_class, suppress_warnings)
        except (errors.LostConnectionError, sqlite3.DatabaseError) as e:
            if self.conn_info['port'] != 'sqlite':
                if not reconnect:
                    raise
                warnings.warn("MySQL server has gone away. Reconnecting to the server.")
                self.connect()
                if self._in_transaction:
                    self.cancel_transaction()
                    raise errors.LostConnectionError("Connection was lost during a transaction.") from None
                logger.debug("Re-executing")
                cursor = self._conn.cursor(cursor=cursor_class)
            else:
                print(e)
                if isinstance(e, sqlite3.OperationalError):
                    # I don't think the stale database connection issue is an OperationalError... but we'll see
                    print(e)
                    raise(e) #upstream will handle commit errors
                elif isinstance(e, sqlite3.IntegrityError):
                    print(e)
                    raise(e) #upstream will handle commit errors
                elif isinstance(e, sqlite3.ProgrammingError):
                    print(e)
                    raise(e)
                if not reconnect:
                    raise
                warnings.warn("SQLite file is probably out of date. Flushing and reconnecting.")
                # not even sure if this is possible with SQLite...
                if self._in_transaction:
                    self.cancel_transaction()
                    raise errors.LostConnectionError("Connection was lost during a transaction.") from None
                self.close()
                self.connect()
                cursor = self._conn.cursor()
            self._execute_query(cursor, query, args, cursor_class, suppress_warnings)
        return cursor

    def get_user(self):
        """
        :return: the user name and host name provided by the client to the server.
        """
        if self.conn_info['port'] == 'sqlite':
            return self.conn_info['user']
        return self.query('SELECT user()').fetchone()[0]

    # ---------- transaction processing
    @property
    def in_transaction(self):
        """
        :return: True if there is an open transaction.
        """
        self._in_transaction = self._in_transaction and self.is_connected
        return self._in_transaction

    def start_transaction(self):
        """
        Starts a transaction error.
        """
        if self.in_transaction:
            raise errors.DataJointError("Nested connections are not supported.")
        if self.conn_info['port'] == 'sqlite':
            self.query('BEGIN TRANSACTION') # seems not as strong as the 'with consistent snapshot' of mysql, but meh
        else:
            self.query('START TRANSACTION WITH CONSISTENT SNAPSHOT')
        self._in_transaction = True
        logger.info("Transaction started")

    def cancel_transaction(self):
        """
        Cancels the current transaction and rolls back all changes made during the transaction.
        """
        try:
            self.query('ROLLBACK')
        except sqlite3.OperationalError as e:
            # sqlite weirdly ends transactions early on its own, so we're
            # catching whether this happens and letting things go as normal...
            # more info: https://docs.python.org/2/library/sqlite3.html#sqlite3-controlling-transactions
            pass
        self._in_transaction = False
        logger.info("Transaction cancelled. Rolling back ...")

    def commit_transaction(self):
        """
        Commit all changes made during the transaction and close it.

        """
        try:
            self.query('COMMIT')
        except sqlite3.OperationalError as e:
            # sqlite weirdly ends transactions early on its own, so we're
            # catching whether this happens and letting things go as normal...
            # more info: https://docs.python.org/2/library/sqlite3.html#sqlite3-controlling-transactions
            pass
        self._in_transaction = False
        logger.info("Transaction committed and closed.")

    # -------- context manager for transactions
    @property
    @contextmanager
    def transaction(self):
        """
        Context manager for transactions. Opens an transaction and closes it after the with statement.
        If an error is caught during the transaction, the commits are automatically rolled back.
        All errors are raised again.

        Example:
        >>> import datajoint as dj
        >>> with dj.conn().transaction as conn:
        >>>     # transaction is open here
        """
        try:
            self.start_transaction()
            yield self
        except:
            self.cancel_transaction()
            raise
        else:
            self.commit_transaction()
