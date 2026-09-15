import json
import logging
import base64
from urllib import request, parse
from django.conf import settings

logger = logging.getLogger(__name__)


def send_sms(phone: str, message: str):
    """Send an SMS and return a tuple `(sent: bool, info: str)`.

    Backends supported:
    - If `SMS_BACKEND` == 'console' -> simulate send and return success.
    - If `SMS_API_URL` is provided -> POST JSON to that URL.
    - Otherwise returns (False, 'no-config').

    This makes it easier to give actionable UI messages when sending fails.
    """
    backend = getattr(settings, 'SMS_BACKEND', '')

    # Console/dev backend: simulate a send
    if backend == 'console':
        logger.info("[SMS console] to=%s message=%s", phone, message)
        return True, 'console'

    # Twilio support (preferred if credentials present)
    tw_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    tw_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    tw_from = getattr(settings, 'TWILIO_FROM', '')
    if tw_sid and tw_token and tw_from:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{tw_sid}/Messages.json"
        data = {
            'To': phone,
            'From': tw_from,
            'Body': message,
        }
        encoded = parse.urlencode(data).encode('utf-8')
        req = request.Request(url, data=encoded, method='POST')
        auth = base64.b64encode(f"{tw_sid}:{tw_token}".encode()).decode()
        req.add_header('Authorization', f'Basic {auth}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with request.urlopen(req, timeout=15) as resp:
                status = resp.getcode()
                body = resp.read().decode('utf-8')
                logger.info('Twilio response: %s %s', status, body)
                if status in (200, 201):
                    return True, f'twilio:{status}'
                return False, f'twilio_http:{status} {body}'
        except Exception as e:
            logger.exception('Twilio error: %s', e)
            return False, f'twilio_exception:{e}'

    # Fallback generic HTTP API
    api_url = getattr(settings, 'SMS_API_URL', '')
    if not api_url:
        logger.info('SMS not configured (no SMS_API_URL and no Twilio credentials)')
        return False, 'no-config'

    payload = {
        'to': phone,
        'message': message,
    }
    sms_from = getattr(settings, 'SMS_FROM', '')
    if sms_from:
        payload['from'] = sms_from

    data = json.dumps(payload).encode('utf-8')
    req = request.Request(api_url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    api_key = getattr(settings, 'SMS_API_KEY', '')
    if api_key:
        req.add_header('Authorization', f'Bearer {api_key}')

    try:
        with request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            body = resp.read().decode('utf-8')
            logger.info('SMS sent: status=%s body=%s', status, body)
            if 200 <= status < 300:
                return True, f'ok:{status}'
            return False, f'http:{status} {body}'
    except Exception as e:
        logger.exception('Error sending SMS: %s', e)
        return False, f'exception:{e}'
