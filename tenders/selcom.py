import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

WALLETS = {
    'MPESA', 'TIGOPESA', 'TIGO', 'AIRTELMONEY', 'AIRTEL', 'HALOPESA',
    'HALO', 'EZYPESA', 'MNO',
}
BANKS = {
    'NMB', 'CRDB', 'NMB_PESA', 'MKURABUCHIT', 'YASB', 'AZANIA', 'KCB',
    'EQUITY', 'DTB', 'BANK',
}

TIMEOUT_SECONDS = 20


class SelcomError(Exception):
    pass


def _headers(token=''):
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _http(method, url, token='', payload=None):
    try:
        return requests.request(method, url, headers=_headers(token), json=payload, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise SelcomError(f'Could not reach Selcom at {url}: {exc}') from exc


def _require_credentials(setting):
    if not setting.selcom_client_id or not setting.selcom_client_secret:
        raise SelcomError('Selcom client ID and secret are not configured under Configuration > Selcom.')
    if not setting.selcom_enabled:
        raise SelcomError('Selcom payments are disabled for this platform.')


def _parse_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _raise_for_http(status_code, text, context):
    if 200 <= status_code < 300:
        return
    detail = (text or '')[:300]
    raise SelcomError(f'Selcom {context} failed (HTTP {status_code}). {detail}')


def get_access_token(setting, force=False):
    _require_credentials(setting)
    url = setting.selcom_api_url('/auth/token')
    response = _http('POST', url, payload={
        'client_id': setting.selcom_client_id,
        'client_secret': setting.selcom_client_secret,
    })
    _raise_for_http(response.status_code, response.text, 'auth')
    data = _parse_json(response.text) or {}
    token = ''
    if isinstance(data, dict):
        nested = data.get('data')
        if isinstance(nested, dict):
            token = str(nested.get('access_token') or '')
        if not token:
            token = str(data.get('access_token') or '')
    if not token:
        raise SelcomError('Selcom auth response did not include an access token.')
    return token


def _payment_dict(parsed):
    if not isinstance(parsed, dict):
        return {}
    data = parsed.get('data')
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if isinstance(data, dict):
        return data
    return parsed


def _pick_status(obj):
    if not isinstance(obj, dict):
        return ''
    for key in ('status', 'payment_status', 'paymentStatus', 'state', 'order_status'):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def is_paid(parsed):
    primary = _payment_dict(parsed)
    candidates = [parsed, primary]
    for obj in list(candidates):
        if isinstance(obj, dict) and isinstance(obj.get('payment'), dict):
            candidates.append(obj['payment'])
    for obj in candidates:
        status = _pick_status(obj).lower()
        if status in ('paid', 'completed', 'complete', 'success', 'successful', 'captured'):
            return True
    return False


def current_status(parsed):
    primary = _payment_dict(parsed)
    for obj in (primary, parsed):
        status = _pick_status(obj)
        if status:
            return status
    return ''


def collected_amount(parsed):
    primary = _payment_dict(parsed)
    candidates = [parsed, primary]
    for obj in list(candidates):
        if isinstance(obj, dict) and isinstance(obj.get('payment'), dict):
            candidates.append(obj['payment'])
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        for key in ('amount', 'paid_amount', 'deposited_amount', 'transaction_amount', 'amount_paid'):
            value = obj.get(key)
            if value is None or str(value).strip() in ('', 'None', 'null'):
                continue
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                continue
    return None


def _classify(methods):
    wallets = []
    banks = []
    for method in methods:
        upper = (method or '').strip().upper()
        if not upper:
            continue
        if upper in WALLETS or upper.endswith('MONEY') or upper.endswith('PESA'):
            wallets.append(upper)
        else:
            banks.append(upper)
    return wallets, banks


def create_checkout_order(setting, invoice, callback_url='', redirect_url=''):
    _require_credentials(setting)
    if invoice.status == invoice.Status.PAID:
        raise SelcomError('This invoice is already paid.')
    token = get_access_token(setting)
    order = invoice.order
    transporter = invoice.transporter
    buyer_name = transporter.company_name if transporter else (order.company_name or '')
    amount = str(invoice.amount_total if invoice.amount_total is not None else 0)
    methods = setting.selcom_methods_list()
    wallets, banks = _classify(methods)
    payload = {
        'vendor': setting.selcom_sales_channel or 'PURCHASE',
        'vendor_reference_id': invoice.selcom_reference or str(invoice.pk),
        'order_id': invoice.number,
        'currency': (setting.selcom_currency or invoice.currency or 'TZS').upper(),
        'amount': amount,
        'buyer_name': buyer_name or 'HYPAX Cargo',
        'buyer_email': order.user.email if order.user else '',
        'buyer_phone': '',
        'wallets': wallets,
        'bank_accounts': banks,
        'no_of_items': invoice.order.lines.filter(awarded=True).count() or 1,
        'request_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'redirect_url': redirect_url or '',
        'callback_url': callback_url or '',
        'items': [
            {
                'name': line.product_name,
                'quantity': str(line.quantity),
                'price': str(line.price_unit),
                'amount': str(line.price_total),
            }
            for line in invoice.order.lines.filter(awarded=True).order_by('line_id')
        ] or [{'name': 'Freight invoice', 'quantity': '1', 'price': amount, 'amount': amount}],
    }
    url = setting.selcom_api_url('/checkout/create-order')
    response = _http('POST', url, token=token, payload=payload)
    _raise_for_http(response.status_code, response.text, 'create-order')
    parsed = _parse_json(response.text) or {}
    data = _payment_dict(parsed)
    order_token = str(data.get('order_token') or data.get('orderToken') or '')
    if not order_token:
        raise SelcomError('Selcom create-order did not return an order token.')
    return {
        'order_token': order_token,
        'pay_link': setting.selcom_payment_link(order_token),
        'reference': invoice.selcom_reference or str(invoice.pk),
        'payment': payload,
        'data': data,
    }


def get_order_status(setting, order_token):
    _require_credentials(setting)
    token = get_access_token(setting)
    url = setting.selcom_api_url('/checkout/get-order-status')
    response = _http('POST', url, token=token, payload={'order_token': order_token})
    _raise_for_http(response.status_code, response.text, 'order-status')
    parsed = _parse_json(response.text) or {}
    return {
        'parsed': parsed,
        'paid': is_paid(parsed),
        'status': current_status(parsed),
    }


def verify_callback(setting, raw_body, header_signature=''):
    if not setting.selcom_webhook_secret:
        return True, 'no webhook secret configured; callback accepted unsafely'
    secret = setting.selcom_webhook_secret.encode('utf-8')
    raw = raw_body if isinstance(raw_body, bytes) else raw_body.encode('utf-8')

    if isinstance(raw_body, str):
        raw_body_encoded = raw_body.encode('utf-8')
    else:
        raw_body_encoded = raw_body

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get('confirm_hash'):
            provided = str(payload.pop('confirm_hash') or '').lower()
            for serialized in (
                json.dumps(payload, separators=(',', ':')),
                json.dumps(payload, separators=(',', ':')).replace(' ', ''),
                json.dumps(ordered_sub(payload), separators=(',', ':')),
            ):
                digest = hashlib.sha256(serialized.encode('utf-8') + secret).hexdigest()
                if hmac.compare_digest(digest, provided):
                    return True, 'confirm_hash verified'
            return False, 'confirm_hash mismatch'
    except (ValueError, TypeError):
        pass

    if header_signature:
        expected_hex = hmac.new(secret, raw_body_encoded, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected_hex, header_signature.strip()):
            return True, 'X-Selcom-Signature (hex) verified'
        import base64
        expected_b64 = base64.b64encode(hmac.new(secret, raw_body_encoded, hashlib.sha256).digest()).decode()
        if hmac.compare_digest(expected_b64, header_signature.strip()):
            return True, 'X-Selcom-Signature (base64) verified'
        return False, 'X-Selcom-Signature mismatch'

    return False, 'no verifiable signature found'


def ordered_sub(value):
    if isinstance(value, dict):
        return {k: ordered_sub(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [ordered_sub(v) for v in value]
    return value