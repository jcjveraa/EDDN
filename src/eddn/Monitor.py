# coding: utf8

"""EDDN Monitor, which receives messages from the Gateway."""
import collections
import datetime
import zlib
from threading import Thread
from typing import Callable, OrderedDict

import gevent
import mysql.connector as mariadb
import simplejson
import zmq.green as zmq
from bottle import Bottle, request, response
from gevent import monkey

from eddn.conf.Settings import Settings, load_config

monkey.patch_all()

app = Bottle()

# This import must be done post-monkey-patching!
if Settings.RELAY_DUPLICATE_MAX_MINUTES:
    from eddn.core.DuplicateMessages import DuplicateMessages
    duplicate_messages = DuplicateMessages()
    duplicate_messages.start()


def date(__format) -> str:
    """
    Make a 'now' datetime as per the supplied format.

    :param __format:
    :return:
    """
    d = datetime.datetime.utcnow()
    return d.strftime(__format)


@app.route('/ping', method=['OPTIONS', 'GET'])
def ping() -> str:
    """Respond to a ping request."""
    return 'pong'


@app.route('/getTotalSoftwares/', method=['OPTIONS', 'GET'])
def get_total_softwares() -> str:
    """Respond with data about total uploading software counts."""
    response.set_header("Access-Control-Allow-Origin", "*")
    db = mariadb.connect(
        user=Settings.MONITOR_DB['user'],
        password=Settings.MONITOR_DB['password'],
        database=Settings.MONITOR_DB['database']
    )
    softwares = collections.OrderedDict()

    max_days = request.GET.get('maxDays', '31').strip()
    max_days = int(max_days) - 1

    query = """SELECT name, SUM(hits) AS total, MAX(dateStats) AS maxDate
               FROM softwares
               GROUP BY name
               HAVING maxDate >= DATE_SUB(NOW(), INTERVAL %s DAY)
               ORDER BY total DESC"""

    results = db.cursor()
    results.execute(query, (max_days, ))

    for row in results:
        softwares[row[0].encode('utf8')] = str(row[1])

    db.close()

    return simplejson.dumps(softwares)


@app.route('/getSoftwares/', method=['OPTIONS', 'GET'])
def get_softwares() -> str:
    """Respond with hit stats for all uploading software."""
    response.set_header("Access-Control-Allow-Origin", "*")
    db = mariadb.connect(
        user=Settings.MONITOR_DB['user'],
        password=Settings.MONITOR_DB['password'],
        database=Settings.MONITOR_DB['database']
    )
    softwares: OrderedDict = collections.OrderedDict()

    date_start = request.GET.get('dateStart', str(date('%Y-%m-%d'))).strip()
    date_end = request.GET.get('dateEnd', str(date('%Y-%m-%d'))).strip()

    query = """SELECT *
               FROM `softwares`
               WHERE `dateStats` BETWEEN %s AND %s
               ORDER BY `hits` DESC, `dateStats` ASC"""

    results = db.cursor()
    results.execute(query, (date_start, date_end))

    for row in results:
        current_date = row[2].strftime('%Y-%m-%d')
        if current_date not in softwares.keys():
            softwares[current_date] = collections.OrderedDict()

        softwares[current_date][str(row[0])] = str(row[1])

    db.close()

    return simplejson.dumps(softwares)


@app.route('/getTotalSchemas/', method=['OPTIONS', 'GET'])
def get_total_schemas() -> str:
    """Respond with total hit stats for all schemas."""
    response.set_header("Access-Control-Allow-Origin", "*")
    db = mariadb.connect(
        user=Settings.MONITOR_DB['user'],
        password=Settings.MONITOR_DB['password'],
        database=Settings.MONITOR_DB['database']
    )
    schemas = collections.OrderedDict()

    query = """SELECT `name`, SUM(`hits`) AS `total`
               FROM `schemas`
               GROUP BY `name`
               ORDER BY `total` DESC"""

    results = db.cursor()
    results.execute(query)

    for row in results:
        schemas[str(row[0])] = row[1]

    db.close()

    return simplejson.dumps(schemas)


@app.route('/getSchemas/', method=['OPTIONS', 'GET'])
def get_schemas() -> str:
    """Respond with schema hit stats between given datetimes."""
    response.set_header("Access-Control-Allow-Origin", "*")
    db = mariadb.connect(
        user=Settings.MONITOR_DB['user'],
        password=Settings.MONITOR_DB['password'],
        database=Settings.MONITOR_DB['database']
    )
    schemas: OrderedDict = collections.OrderedDict()

    date_start = request.GET.get('dateStart', str(date('%Y-%m-%d'))).strip()
    date_end = request.GET.get('dateEnd', str(date('%Y-%m-%d'))).strip()

    query = """SELECT *
               FROM `schemas`
               WHERE `dateStats` BETWEEN %s AND %s
               ORDER BY `hits` DESC, `dateStats` ASC"""

    results = db.cursor()
    results.execute(query, (date_start, date_end))

    for row in results:
        current_date = row[2].strftime('%Y-%m-%d')
        if current_date not in schemas.keys():
            schemas[current_date] = collections.OrderedDict()

        schemas[current_date][str(row[0])] = str(row[1])

    db.close()

    return simplejson.dumps(schemas)


class Monitor(Thread):
    """Monitor thread class."""

    def run(self) -> None:
        """Handle receiving Gateway messages and recording stats."""
        context = zmq.Context()

        receiver = context.socket(zmq.SUB)
        receiver.setsockopt(zmq.SUBSCRIBE, '')

        for binding in Settings.MONITOR_RECEIVER_BINDINGS:
            receiver.connect(binding)

        def monitor_worker(message: bytes) -> None:
            db = mariadb.connect(
                user=Settings.MONITOR_DB['user'],
                password=Settings.MONITOR_DB['password'],
                database=Settings.MONITOR_DB['database']
            )

            # Separate topic from message
            message_split = message.split(b' |-| ')

            # Handle gateway not sending topic
            if len(message_split) > 1:
                message = message_split[1]
            else:
                message = message_split[0]

            message_text = zlib.decompress(message)
            json = simplejson.loads(message_text)

            # Default variables
            schema_id = json['$schemaRef']
            software_id = json['header']['softwareName'].encode('utf8') + ' | ' \
                + json['header']['softwareVersion'].encode('utf8')

            # Duplicates?
            if Settings.RELAY_DUPLICATE_MAX_MINUTES:
                if duplicate_messages.is_duplicated(json):
                    schema_id = 'DUPLICATE MESSAGE'

                    c = db.cursor()
                    c.execute(
                        'UPDATE `schemas` SET `hits` = `hits` + 1 WHERE `name` = %s AND `dateStats` = UTC_DATE()',
                        (schema_id, )
                    )
                    c.execute(
                        'INSERT IGNORE INTO `schemas` (`name`, `dateStats`) VALUES (%s, UTC_DATE())',
                        (schema_id, )
                    )
                    db.commit()

                    db.close()

                    return

            # Update software count
            c = db.cursor()
            c.execute(
                'UPDATE `softwares` SET `hits` = `hits` + 1 WHERE `name` = %s AND `dateStats` = UTC_DATE()',
                (software_id, )
            )
            c.execute(
                'INSERT IGNORE INTO `softwares` (`name`, `dateStats`) VALUES (%s, UTC_DATE())',
                (software_id, )
            )
            db.commit()

            # Update schemas count
            c = db.cursor()
            c.execute(
                'UPDATE `schemas` SET `hits` = `hits` + 1 WHERE `name` = %s AND `dateStats` = UTC_DATE()',
                (schema_id, )
            )
            c.execute(
                'INSERT IGNORE INTO `schemas` (`name`, `dateStats`) VALUES (%s, UTC_DATE())',
                (schema_id, )
            )
            db.commit()

            db.close()

        while True:
            inbound_message = receiver.recv()
            gevent.spawn(monitor_worker, inbound_message)


class EnableCors(object):
    """Enable CORS responses."""

    name = 'enable_cors'
    api = 2

    @staticmethod
    def apply(self, fn: Callable, context: str):
        """
        Apply a CORS handler.

        Ref: <https://stackoverflow.com/a/17262900>
        """
        def _enable_cors(*args, **kwargs):
            """Set CORS Headers."""
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = \
                'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'

            if request.method != 'OPTIONS':
                # actual request; reply with the actual response
                return fn(*args, **kwargs)

        return _enable_cors


def main() -> None:
    """Handle setting up and running the bottle app."""
    load_config()
    m = Monitor()
    m.start()
    app.install(EnableCors())
    app.run(
        host=Settings.MONITOR_HTTP_BIND_ADDRESS,
        port=Settings.MONITOR_HTTP_PORT,
        server='gevent',
        certfile=Settings.CERT_FILE,
        keyfile=Settings.KEY_FILE
    )


if __name__ == '__main__':
    main()
