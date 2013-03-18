
import json
import datetime
import pytz
import re

from collections import defaultdict

from flask import request, current_app, send_from_directory
from functools import wraps
from alerta.api.v2 import app, db, create_mq

from alerta.common import config
from alerta.common import log as logging
from alerta.alert import Alert, severity, status, ATTRIBUTES
from alerta.common.utils import DateEncoder

Version = '2.0.0'

LOG = logging.getLogger(__name__)
CONF = config.CONF

# TODO(nsatterl): put these constants somewhere appropriate
_MAX_HISTORY = -10  # 10 most recent
_LIMIT = 100


# Over-ride jsonify to support Date Encoding
def jsonify(*args, **kwargs):
    return current_app.response_class(json.dumps(dict(*args, **kwargs), cls=DateEncoder,
                                                 indent=None if request.is_xhr else 2), mimetype='application/json')


# TODO(nsatterl): use @before_request and @after_request to attach a unique request id
# @app.before_request
# def before_request():
#     pass

def jsonp(func):
    """Wraps JSONified output for JSONP requests."""
    @wraps(func)
    def decorated_function(*args, **kwargs):
        callback = request.args.get('callback', False)
        if callback:
            data = str(func(*args, **kwargs).data)
            content = str(callback) + '(' + data + ')'
            mimetype = 'application/javascript'
            return current_app.response_class(content, mimetype=mimetype)
        else:
            return func(*args, **kwargs)
    return decorated_function


@app.route('/test', methods=['POST', 'GET', 'PUT', 'DELETE'])
def test():

    return jsonify(response={"status": "ok", "json": request.json, "data": request.data, "args": request.args})


# Returns a list of alerts
@app.route('/alerta/api/v2/alerts', methods=['GET'])
@jsonp
def get_alerts():

    query = dict()
    query_time = datetime.datetime.utcnow()
    from_date = request.args.get('from-date', None)
    if from_date:
        from_date = datetime.datetime.strptime(from_date, '%Y-%m-%dT%H:%M:%S.%fZ')
        from_date = from_date.replace(tzinfo=pytz.utc)
        to_date = query_time
        to_date = to_date.replace(tzinfo=pytz.utc)
        query['lastReceiveTime'] = {'$gt': from_date, '$lte': to_date }

    if request.args.get('id', None):
        query['_id'] = dict()
        query['_id']['$regex'] = '^' + request.args['id']

    for field in [fields for fields in request.args if fields in ATTRIBUTES]:
        value = request.args.getlist(field)
        LOG.error('field (%s) = %s', field, value)
        if len(value) == 1:
            if field.startswith('-'):
                query[field[1:]] = dict()
                query[field[1:]]['$not'] = re.compile(value[0])
            else:
                query[field] = dict()
                query[field]['$regex'] = value[0]
                query[field]['$options'] = 'i'  # case insensitive search
        else:
            if field.startswith('-'):
                query[field[1:]] = dict()
                query[field[1:]]['$nin'] = value
            else:
                query[field] = dict()
                query[field]['$in'] = value

    sort = list()
    if request.args.get('sort-by', None):
        for sort_by in request.args.getlist('sort-by'):
            if sort_by in ['createTime', 'receiveTime', 'lastReceiveTime']:
                sort.append((sort_by, -1))  # sort by newest first
            else:
                sort.append((sort_by, 1))  # sort by newest first
    else:
        sort.append(('lastReceiveTime', -1))

    limit = request.args.get('limit', _LIMIT, int)

    alerts = db.get_alerts(query=query, sort=sort, limit=limit)
    total = db.get_count(query=query)  # TODO(nsatterl): possible race condition?

    found = 0
    alert_details = list()
    if len(alerts) > 0:

        severity_count = defaultdict(int)
        status_count = defaultdict(int)
        last_time = None

        for alert in alerts:
            body = alert.get_body()

            if body['severity'] in request.args.getlist('hide-alert-repeats') and body['repeat']:
                continue

            if not request.args.get('hide-alert-details', False, bool):
                alert_details.append(body)

            if request.args.get('hide-alert-history', False, bool):
                body['history'] = []

            found += 1
            severity_count[body['severity']] += 1
            status_count[body['status']] += 1

            if not last_time:
                last_time = body['lastReceiveTime']
            elif body['lastReceiveTime'] > last_time:
                last_time = body['lastReceiveTime']

        return jsonify(response={
            "alerts": {
                "alertDetails": alert_details,
                "severityCounts": {
                    "critical": severity_count[severity.CRITICAL],
                    "major": severity_count[severity.MAJOR],
                    "minor": severity_count[severity.MINOR],
                    "warning": severity_count[severity.WARNING],
                    "indeterminate": severity_count[severity.INDETERMINATE],
                    "cleared": severity_count[severity.CLEARED],
                    "normal": severity_count[severity.NORMAL],
                    "informational": severity_count[severity.INFORM],
                    "debug": severity_count[severity.DEBUG],
                    "auth": severity_count[severity.AUTH],
                    "unknown": severity_count[severity.UNKNOWN],
                },
                "statusCounts": {
                    "open": status_count[status.OPEN],
                    "ack": status_count[status.ACK],
                    "closed": status_count[status.CLOSED],
                    "expired": status_count[status.EXPIRED],
                    "unknown": status_count[status.UNKNOWN],
                },
                "lastTime": last_time,
            },
            "status": "ok",
            "total": found,
            "more": total > limit
        })
    else:
        return jsonify(response={
            "alerts": {
                "alertDetails": list(),
                "severityCounts": {
                    "critical": 0,
                    "major": 0,
                    "minor": 0,
                    "warning": 0,
                    "indeterminate": 0,
                    "cleared": 0,
                    "normal": 0,
                    "informational": 0,
                    "debug": 0,
                    "auth": 0,
                    "unknown": 0,
                    },
                "statusCounts": {
                    "open": 0,
                    "ack": 0,
                    "closed": 0,
                    "expired": 0,
                    "unknown": 0,
                    },
                "lastTime": query_time,
            },
            "status": "error",
            "error": "not found",
            "total": 0,
            "more": False,
        })


@app.route('/alerta/api/v2/alerts/alert.json', methods=['POST'])
@jsonp
def create_alert():

    # Create a new alert
    try:
        alert = json.loads(request.data)
    except Exception, e:
        return jsonify(response={"status": "error", "message": e})

    newAlert = Alert(
        resource=alert.get('resource', None),
        event=alert.get('event', None),
        correlate=alert.get('correlatedEvents', None),
        group=alert.get('group', None),
        value=alert.get('value', None),
        severity=severity.parse_severity(alert.get('severity', None)),
        environment=alert.get('environment', None),
        service=alert.get('service', None),
        text=alert.get('text', None),
        event_type=alert.get('type', 'exceptionAlert'),
        tags=alert.get('tags', None),
        origin=alert.get('origin', None),
        threshold_info=alert.get('thresholdInfo', None),
        timeout=alert.get('timeout', None),
        raw_data=alert.get('rawData', None),
    )
    LOG.debug('New alert %s', newAlert)
    create_mq.send(newAlert)

    if newAlert:
        return jsonify(response={"status": "ok", "id": newAlert.get_id()})
    else:
        return jsonify(response={"status": "error", "message": "something went wrong"})


@app.route('/alerta/api/v2/alerts/alert/<alertid>', methods=['GET', 'PUT', 'POST', 'DELETE'])
@jsonp
def rud_alert(alertid):

    error = None

    # Return a single alert
    if request.method == 'GET':
        alert = db.get_alert(alertid=alertid)
        if alert:
            return jsonify(response={"alert": alert.get_body(), "status": "ok", "total": 1})
        else:
            return jsonify(response={"alert": None, "status": "error", "message": "not found", "total": 0})

    # Update a single alert
    elif request.method == 'PUT':
        if request.json:
            response = db.partial_update_alert(alertid, update=request.json)
        else:
            response = None
            error = "no post data"

        if response:
            return jsonify(response={"status": "ok"})
        else:
            return jsonify(response={"status": "error", "message": error})

    # Delete a single alert
    elif request.method == 'DELETE' or (request.method == 'POST' and request.json['_method'] == 'delete'):
        response = db.delete_alert(alertid)

        if response:
            return jsonify(response={"status": "ok"})
        else:
            return jsonify(response={"status": "error", "message": error})

    else:
        return jsonify(response={"status": "error", "message": "POST request without '_method' override?"})


# Tag an alert
@app.route('/alerta/api/v2/alerts/alert/<alertid>/tag', methods=['PUT'])
@jsonp
def tag_alert(alertid):

    tag = request.json

    if tag:
        response = db.tag_alert(alertid, tag['tag'])
    else:
        return jsonify(response={"status": "error", "message": "no data"})

    if response:
        return jsonify(response={"status": "ok"})
    else:
        return jsonify(response={"status": "error", "message": "error tagging alert"})


@app.route('/alerta/dashboard/<path:filename>')
def console(filename):
    return send_from_directory(CONF.dashboard_dir, filename)

