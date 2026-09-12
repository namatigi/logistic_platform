import concurrent.futures
import json
import logging
import math
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests as http
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Count
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import (
    IncomingEmailConfigForm,
    MapConfigForm,
    MediaConfigForm,
    OdooCompanyConfigForm,
    OutgoingEmailConfigForm,
    PaymentTermForm,
    SelcomConfigForm,
    TenderForm,
)
from .context_processors import MAP_CONFIG_CACHE_KEY
from .models import (
    ApiDiagnostic,
    ApiSetting,
    EscrowAccount,
    Invoice,
    OdooCompany,
    Order,
    OrderLine,
    PaymentTerm,
    PendingPush,
    Tender,
    TenderSubmission,
    Town,
    Transporter,
    generate_transporter_alias,
)
from . import selcom as selcom
from .towns import TOWN_CHOICES
from companies.models import Company
from users.models import CustomUser

import functools

logger = logging.getLogger(__name__)


def _admin_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if getattr(request.user, 'role', None) != CustomUser.Role.ADMINISTRATOR:
            return redirect('tenders:dashboard')
        return view(request, *args, **kwargs)
    return wrapper


def _record_diagnostic(request, api_point, message, status_code=None, path='', method='', detail=None):
    """Persist an error produced by an API point so administrators can review it."""
    user = request.user if request is not None and getattr(request.user, 'is_authenticated', False) else None
    try:
        ApiDiagnostic.objects.create(
            user=user,
            api_point=api_point,
            method=str(method or ''),
            path=str(path or ''),
            status_code=status_code,
            message='' if message is None else str(message),
            detail=detail,
        )
        if request is not None:
            request._api_diagnostic_logged = True
    except Exception:
        logger.exception('Failed to record API diagnostic for %s', api_point)


def _platform_setting():
    """Return the single platform-wide setting (Selcom, email, file storage)."""
    return ApiSetting.get()


def _order_endpoint(order):
    """Return the Odoo company (transporter) an order belongs to, or None.

    Orders are received through a company-specific webhook, so every order has a
    transporter and its confirmations go back to that transporter only.
    """
    if order is not None and order.odoo_company_id:
        company = order.odoo_company
        if company.base_url:
            return company
    return None


def _available_transporters():
    """Transport companies (Odoo instances) with a configured base URL.

    Registering a company on the Companies page adds a *customer* company (who we
    ship cargo for) — it has no transporter link. Every tender is therefore sent to
    ALL configured transport companies.
    """
    return list(OdooCompany.objects.filter(is_active=True, base_url__gt=''))


def _tender_submission_setting(user):
    """Return a configured transport company, or None. Used only to tell the form
    whether any tender target exists — submissions go to all of them."""
    available = _available_transporters()
    return available[0] if available else None


class AdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return getattr(self.request.user, 'role', None) == CustomUser.Role.ADMINISTRATOR


class TenderCreatorRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return getattr(self.request.user, 'role', None) != CustomUser.Role.AGENT


class AgentRedirectMixin(UserPassesTestMixin):
    def test_func(self):
        return getattr(self.request.user, 'role', None) != CustomUser.Role.AGENT

    def handle_no_permission(self):
        if getattr(self.request.user, 'role', None) == CustomUser.Role.AGENT:
            return redirect('users:agent_awarded')
        return super().handle_no_permission()


def get_or_create_transporter(order):
    company_id = order.company_id
    company_name = (order.company_name or '').strip()
    transporter = None
    if company_name:
        transporter = Transporter.objects.filter(company_name__iexact=company_name).first()
    if transporter is None:
        transporter = Transporter.objects.create(
            company_id=company_id or None,
            company_name=company_name,
            alias=order.transporter_alias or '',
        )
    else:
        update_fields = []
        if company_id and transporter.company_id != company_id:
            transporter.company_id = company_id
            update_fields.append('company_id')
        if company_name and transporter.company_name != company_name:
            transporter.company_name = company_name
            update_fields.append('company_name')
        if transporter.alias != order.transporter_alias:
            transporter.alias = order.transporter_alias or ''
            update_fields.append('alias')
        if update_fields:
            transporter.save(update_fields=update_fields)
    return transporter


def get_or_create_invoice(order):
    invoice = Invoice.objects.filter(order=order).first()
    if invoice is None:
        transporter = get_or_create_transporter(order)
        number = f'INV-{order.order_id}'
        if Invoice.objects.filter(number=number).exists():
            number = f'INV-{order.order_id}-{order.pk}'
        invoice = Invoice.objects.create(
            number=number,
            order=order,
            transporter=transporter,
            amount_total=order.awarded_amount,
            currency=order.currency or 'TZS',
        )
    _ensure_escrow_account(order.tender, order, invoice)
    return invoice


def _ensure_escrow_account(tender, order, invoice):
    if tender is None:
        return None
    escrow = EscrowAccount.objects.filter(tender=tender).select_related('user').first()
    update_fields = []
    if escrow is None:
        escrow = EscrowAccount.objects.create(
            tender=tender,
            user=tender.user,
            amount=invoice.amount_total if invoice else Decimal('0.00'),
            payment_terms=tender.payment_terms,
            bank='Selcom',
        )
        escrow.virtual_account = f'EA-{escrow.pk:05d}'
        escrow.save(update_fields=('virtual_account',))
    else:
        if escrow.payment_terms_id is None and tender.payment_terms_id:
            escrow.payment_terms = tender.payment_terms
            update_fields.append('payment_terms')
        if update_fields:
            escrow.save(update_fields=update_fields)
    if invoice is not None:
        if invoice.transporter_id and not escrow.transporters.filter(pk=invoice.transporter_id).exists():
            escrow.transporters.add(invoice.transporter)
        if not escrow.invoices.filter(pk=invoice.pk).exists():
            escrow.invoices.add(invoice)
    _refresh_escrow(escrow)
    return escrow


def _refresh_escrow(escrow):
    invoices = Invoice.objects.filter(order__tender=escrow.tender)
    linked_invoice_ids = set(escrow.invoices.values_list('pk', flat=True))
    missing_invoice_ids = set(invoices.values_list('pk', flat=True)) - linked_invoice_ids
    if missing_invoice_ids:
        escrow.invoices.add(*missing_invoice_ids)
    linked_transporter_ids = set(escrow.transporters.values_list('pk', flat=True))
    missing_transporter_ids = {
        inv.transporter_id for inv in invoices if inv.transporter_id and inv.transporter_id not in linked_transporter_ids
    }
    if missing_transporter_ids:
        escrow.transporters.add(*missing_transporter_ids)
    expected = invoices.aggregate(total=Sum('amount_total'))['total'] or Decimal('0.00')
    deposited = invoices.aggregate(total=Sum('deposited_amount'))['total'] or Decimal('0.00')
    has_checkout = invoices.exclude(selcom_order_token='').exists()
    total_invoices = invoices.count()
    paid_invoices = invoices.filter(status=Invoice.Status.PAID).count()
    if total_invoices and paid_invoices == total_invoices:
        status = EscrowAccount.Status.PAID
    elif has_checkout or paid_invoices > 0:
        status = EscrowAccount.Status.PENDING
    else:
        status = EscrowAccount.Status.OPEN
    updated = []
    if escrow.amount != expected:
        escrow.amount = expected
        updated.append('amount')
    if escrow.deposited_amount != deposited:
        escrow.deposited_amount = deposited
        updated.append('deposited_amount')
    if escrow.status != status:
        escrow.status = status
        updated.append('status')
    if updated:
        escrow.save(update_fields=updated)


def notify_order_update(order):
    _flush_derived_caches()
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    from asgiref.sync import async_to_sync
    send = async_to_sync(channel_layer.group_send)

    def push(group, data):
        try:
            send(group, data)
        except Exception:
            logger.warning('WebSocket push to group %s failed', group)

    payload = {'type': 'order.update', 'data': {'action': 'refresh'}}
    push('orders_admins', payload)
    if order.user_id:
        push(f'orders_user_{order.user_id}', payload)
    else:
        push('orders_all', payload)
    push(
        f'order_{order.pk}',
        {'type': 'order.update', 'data': _order_dict(order, include_lines=True)},
    )


def build_payload(tender):
    payment_term = tender.payment_terms if tender.payment_terms_id else None
    reference = tender.tender_reference()
    payload = {
        'route_loading': tender.route_loading,
        'route_delivery': tender.route_delivery,
        'customer': 'HYPAX',
        'cargo_type': tender.cargo_type,
        'truck_type': tender.truck_type,
        'weight': tender.weight,
        'number_of_trucks': tender.number_of_trucks,
        'distance_km': tender.distance_km,
        'cargo_date': tender.cargo_date.isoformat(),
        'tender_reference': reference,
    }
    if payment_term is not None:
        payload['payment_terms'] = {
            'name': payment_term.name,
            'description': payment_term.description,
            'items': [{'text': item.text} for item in payment_term.items.all()],
        }
    else:
        payload['payment_terms'] = None
    return payload


def _build_auth(setting):
    headers = {'Content-Type': 'application/json'}
    auth = None
    if setting.auth_type == ApiSetting.AuthType.BEARER and setting.api_token:
        headers['Authorization'] = f'Bearer {setting.api_token}'
    elif setting.auth_type == ApiSetting.AuthType.BASIC:
        auth = (setting.username, setting.password)
    return headers, auth


_HTTP_RETRIES = 1
_HTTP_RETRY_DELAY_SECONDS = 2


def _post_with_retry(url, payload, headers, auth):
    """POST a JSON payload, retrying once on transport-level (connection) errors.

    HTTP error responses (4xx/5xx) are returned as-is and never retried, so a
    request that might already have reached the external system is not repeated.
    """
    last_error = None
    attempts = _HTTP_RETRIES + 1
    for attempt in range(attempts):
        try:
            response = http.post(url, json=payload, headers=headers, auth=auth, timeout=20)
            return response.status_code, response.text, response.ok
        except http.RequestException as exc:
            last_error = exc
            logger.warning('POST to %s failed (attempt %d/%d): %s', url, attempt + 1, attempts, exc)
            if attempt < _HTTP_RETRIES:
                time.sleep(_HTTP_RETRY_DELAY_SECONDS)
    return None, str(last_error), False


def _describe_connection_error(body):
    text = str(body)
    match = re.search(r'\[Errno\s+(\d+)\]', text)
    if match:
        code = match.group(1)
        if code == '111':
            return 'connection refused (the Odoo instance is not accepting connections)'
        if code in ('110', '10060'):
            return 'connection timed out'
        if code in ('-2', '-3'):
            return 'the host name could not be resolved'
        return f'connection error (Errno {code})'
    if 'NameResolutionError' in text or 'Failed to resolve' in text:
        return 'the host name could not be resolved'
    if 'Max retries exceeded' in text:
        return 'network error when reaching the Odoo instance'
    if 'ConnectionError' in text:
        return 'connection error'
    return 'a network error occurred'


def _submit_failure_message(label, status_code, body):
    if status_code is None:
        issue = _describe_connection_error(body)
        return (
            f'{label} failed: {issue}. '
            f'Could not reach the Odoo instance — check the base URL and that the instance is online, '
            f'then submit again.'
        )
    return f'{label} failed (HTTP {status_code}). Response: {body[:300]}'


def submit_tender(setting, tender):
    payload = build_payload(tender)
    headers, auth = _build_auth(setting)
    return _post_with_retry(setting.endpoint_url(), payload, headers, auth)


def submit_confirmation(setting, url, payload):
    headers, auth = _build_auth(setting)
    return _post_with_retry(url, payload, headers, auth)


def _record_submission(tender, company, status_code, body):
    """Store one transport company's result for a tender. Returns (is_ok, parsed_data)."""
    parsed = {}
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, TypeError):
        pass
    data = parsed.get('data') if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        data = {}
    is_ok = status_code is not None and 200 <= status_code < 300
    if not is_ok and isinstance(parsed, dict) and parsed.get('status') == 'success':
        is_ok = True
    TenderSubmission.objects.create(
        tender=tender,
        odoo_company=company,
        status_code=status_code,
        response_body=(body or '')[:4000],
        external_id=data.get('id'),
        trans_reference=data.get('name', '') or '',
        external_status=data.get('status', '') or '',
        success=is_ok,
    )
    return is_ok, data


def _apply_aggregate_tender(tender):
    """Copy the best (first successful, else last) submission onto the tender."""
    success_sub = tender.submissions.filter(success=True).order_by('created_at').first()
    sub = success_sub or tender.submissions.order_by('-created_at').first()
    if sub is not None:
        tender.response_code = sub.status_code
        tender.response_body = sub.response_body
        tender.external_id = sub.external_id
        tender.trans_reference = sub.trans_reference
        tender.external_status = sub.external_status
        tender.status = Tender.Status.SUCCESS if sub.success else Tender.Status.FAILED
    else:
        tender.status = Tender.Status.PENDING
    tender.save()
    return tender


def _submit_tender_to_targets(tender, targets=None):
    """Submit a tender to every configured transport company and record results.

    Returns {'targets': [...], 'succeeded': int, 'failed': int}.
    """
    if targets is None:
        targets = _available_transporters()
    tender.ensure_reference()
    for company in targets:
        status_code, body, _ok = submit_tender(company, tender)
        _record_submission(tender, company, status_code, body)
    _apply_aggregate_tender(tender)
    return {
        'targets': targets,
        'succeeded': tender.submissions.filter(success=True).count(),
        'failed': len(targets),
    }


def _has_transient_failure(tender):
    """True when any target failed with a connection error or 5xx.

    Such a failure is worth retrying, even when another target already accepted
    the tender (or permanently rejected it with a 4xx).
    """
    return any(
        not submission.success
        and (submission.status_code is None or submission.status_code >= 500)
        for submission in tender.submissions.all()
    )


def _enqueue_tender(tender, error_message):
    """Queue a failed tender submission so it is re-sent when the API is back up."""
    existing = PendingPush.objects.filter(
        tender=tender, state=PendingPush.State.PENDING,
    ).first()
    if existing is not None:
        return existing
    return PendingPush.objects.create(tender=tender, last_error=error_message)


def _deliver_push(push, targets):
    """Attempt one delivery of a queued tender to the given transport companies.

    The push is only resolved once every target has either accepted the tender or
    permanently rejected it (4xx). Returns True when the tender has been delivered
    to at least one target, False otherwise. While any target is still unreachable
    (connection error / 5xx) the push stays PENDING and is retried later.
    """
    push.attempts += 1
    push.last_attempt_at = timezone.now()
    push.tender.ensure_reference()
    transient = 0
    last_error = ''
    last_code = None
    for company in targets:
        status_code, body, _ok = submit_tender(company, push.tender)
        _record_submission(push.tender, company, status_code, body)
        if _response_ok(status_code, body):
            last_code = status_code
        elif status_code is not None and status_code < 500:
            last_code = status_code
        else:
            transient += 1
            last_code = status_code
            last_error = _submit_failure_message('Tender submission', status_code, body)
    _apply_aggregate_tender(push.tender)
    if last_code is not None:
        push.response_code = last_code

    if transient:
        # Some targets are still unreachable — keep the push queued.
        push.last_error = last_error or push.last_error
        push.save()
        return False

    if push.tender.submissions.filter(success=True).exists():
        push.state = PendingPush.State.DELIVERED
        push.last_error = ''
        push.delivered_at = timezone.now()
        push.save()
        _flush_derived_caches()
        return True

    # Every target permanently rejected it.
    push.state = PendingPush.State.FAILED
    push.last_error = last_error or _submit_failure_message('Tender submission', push.response_code, '')
    _record_diagnostic(
        None, 'tender.relay', push.last_error, status_code=push.response_code, method='POST',
        path=targets[0].endpoint_url() if targets else '',
        detail={'tender_id': push.tender_id, 'pending_push': push.pk},
    )
    push.save()
    return False


def _response_ok(status_code, body):
    is_ok = status_code is not None and 200 <= status_code < 300
    if not is_ok:
        try:
            parsed = json.loads(body) if body else {}
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict) and parsed.get('status') == 'success':
            is_ok = True
    return is_ok


def _undelivered_targets(tender, targets):
    """Targets that have neither accepted the tender nor permanently rejected it."""
    undelivered = []
    for company in targets:
        submissions = tender.submissions.filter(odoo_company=company)
        if submissions.filter(success=True).exists():
            continue
        latest = submissions.order_by('-created_at').first()
        if latest is not None and latest.status_code is not None and latest.status_code < 500:
            # Permanently rejected on the latest attempt; retrying will not help.
            continue
        undelivered.append(company)
    return undelivered


def _attempt_push(push):
    """Try to deliver one queued push. Returns (delivered, failed, skipped)."""
    targets = _available_transporters()
    if not targets:
        return 0, 0, 1
    pending_targets = _undelivered_targets(push.tender, targets)
    if not pending_targets:
        # Every target already accepted or permanently rejected the tender.
        if push.tender.submissions.filter(success=True).exists():
            push.state = PendingPush.State.DELIVERED
            push.delivered_at = timezone.now()
            push.save()
            return 1, 0, 0
        push.state = PendingPush.State.FAILED
        push.save()
        return 0, 1, 0
    with transaction.atomic():
        if _deliver_push(push, pending_targets):
            return 1, 0, 0
        if push.state == PendingPush.State.FAILED:
            return 0, 1, 0
    return 0, 0, 0


def flush_pending_pushes(user=None):
    """Re-send queued tender submissions. Returns (delivered, permanently_failed, skipped)."""
    delivered = failed = skipped = 0
    queryset = PendingPush.objects.select_related('tender', 'tender__user').filter(
        state=PendingPush.State.PENDING,
    )
    if user is not None:
        queryset = queryset.filter(tender__user=user)
    for push in queryset.order_by('created_at'):
        attempt_delivered, attempt_failed, attempt_skipped = _attempt_push(push)
        delivered += attempt_delivered
        failed += attempt_failed
        skipped += attempt_skipped
    return delivered, failed, skipped


def force_retry_pushes(push_ids=None, user=None):
    """Manually force pending and permanently failed pushes to be re-sent now.

    Failed pushes are re-queued first so they are retried even after a 4xx.
    Returns (delivered, failed_again, skipped).
    """
    queryset = PendingPush.objects.select_related('tender', 'tender__user').filter(
        state__in=(PendingPush.State.PENDING, PendingPush.State.FAILED),
    )
    if push_ids is not None:
        queryset = queryset.filter(pk__in=push_ids)
    if user is not None:
        queryset = queryset.filter(tender__user=user)
    delivered = failed = skipped = 0
    for push in queryset.order_by('created_at'):
        if push.state == PendingPush.State.FAILED:
            push.state = PendingPush.State.PENDING
            push.last_error = ''
            push.save(update_fields=('state', 'last_error'))
        attempt_delivered, attempt_failed, attempt_skipped = _attempt_push(push)
        delivered += attempt_delivered
        failed += attempt_failed
        skipped += attempt_skipped
    return delivered, failed, skipped


def perform_award(order, setting, line_ids, is_partial):
    if is_partial and not line_ids:
        return {'ok': False, 'message': 'Select at least one order line to award.'}

    if line_ids:
        valid_ids = set(OrderLine.objects.filter(order=order).values_list('line_id', flat=True))
        if not set(line_ids).issubset(valid_ids):
            return {'ok': False, 'message': 'Some selected order lines do not belong to this order.'}

    if line_ids:
        extra = OrderLine.objects.filter(order=order, line_id__in=line_ids).exclude(awarded=True).count()
    else:
        extra = order.lines.exclude(awarded=True).count()
    quota_error = _truck_quota_exceeded(order, extra=extra)
    if quota_error:
        return {'ok': False, 'message': quota_error}

    cargo_name = (
        (order.tender.tender_reference() if order.tender else '')
        or order.trans_reference
        or ''
    ).strip()
    if not cargo_name:
        return {'ok': False, 'message': 'This order has no reference to confirm.'}

    payload = {
        'order_id': order.order_id,
        'message': 'Confirmed',
        'cargo_name': cargo_name,
        'tender_reference': order.tender.tender_reference() if order.tender else '',
    }
    if line_ids:
        payload['order_lines'] = [{'line_id': lid} for lid in line_ids]

    url = (
        setting.partial_order_confirmation_url()
        if line_ids
        else setting.order_confirmation_url()
    )
    status_code, body, _ok = submit_confirmation(setting, url, payload)

    parsed = {}
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, TypeError):
        pass

    is_ok = status_code is not None and 200 <= status_code < 300
    if isinstance(parsed, dict) and parsed.get('status') == 'success':
        is_ok = True

    if not is_ok:
        return {
            'ok': False,
            'message': _submit_failure_message('Order confirmation', status_code, body),
            'url': url,
            'status_code': status_code,
            'response': body[:4000],
        }

    order.award_response = parsed if isinstance(parsed, dict) else {}
    order.awarded_at = timezone.now()
    if line_ids:
        OrderLine.objects.filter(order=order, line_id__in=line_ids).update(awarded=True)
    else:
        order.lines.update(awarded=True)

    new_total = order.awarded_amount
    if new_total != order.amount_total:
        order.amount_total = new_total

    if order.state == 'draft':
        order.state = 'confirmed'
    order.save()

    notify_order_update(order)

    message = parsed.get('message') if isinstance(parsed, dict) else None
    if not message:
        message = 'Order partially confirmed.' if line_ids else 'Order confirmed successfully.'
    return {'ok': True, 'message': message, 'order': order}


@login_required
def award_order(request, pk):
    order = Order.objects.filter(
        Q(user=request.user) | Q(user__isnull=True)
    ).filter(pk=pk).first()
    if order is None:
        raise Http404()

    setting = _order_endpoint(order)
    if setting is None:
        messages.warning(
            request,
            'This order has no Odoo company (transporter) with a base URL, so confirmations cannot be sent. '
            'An administrator must update the transporter that posted the order.',
        )
        return redirect('tenders:config_odoo')

    line_ids_raw = request.POST.getlist('line_ids')
    line_ids = [int(v) for v in line_ids_raw if str(v).strip().isdigit()]
    is_partial = request.POST.get('partial') == '1'
    result = perform_award(order, setting, line_ids, is_partial)
    if not result['ok']:
        _record_diagnostic(
            request, 'order.award', result['message'], status_code=result.get('status_code'), method='POST',
            path=result.get('url', ''),
            detail={'order_id': order.order_id, 'response': result.get('response', '')},
        )
    (messages.success if result['ok'] else messages.error)(request, result['message'])

    if line_ids:
        return redirect('tenders:order_detail', pk=order.pk)
    return redirect('tenders:order_list')


@login_required
def make_payment(request, pk):
    order = Order.objects.filter(
        Q(user=request.user) | Q(user__isnull=True)
    ).filter(pk=pk).first()
    if order is None:
        raise Http404()
    messages.info(request, f'Payment for order {order.order_name or order.order_id} is not available yet.')
    return redirect('tenders:order_detail', pk=order.pk)


def parse_datetime(value):
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%Y-%m-%d %H:%M:%S%z'):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = timezone.make_aware(dt)
            return dt
        except ValueError:
            continue
    return None


def to_decimal(value):
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return Decimal('0')


@csrf_exempt
def webhook_orders(request, slug):
    """Company-specific order webhook: orders are always attributed to their transporter.

    There is no shared/legacy webhook any more — every order must be sent to its
    transporter's own URL `.../webhook/orders/<slug>/`.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST is allowed.'}, status=405)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)
    if not isinstance(payload, dict) or 'order_id' not in payload:
        return JsonResponse({'error': "Missing required field 'order_id'."}, status=400)

    company = OdooCompany.objects.filter(slug=slug).first()
    if company is None:
        return JsonResponse({'error': f"Unknown company webhook '{slug}'."}, status=404)
    if not company.is_active:
        return JsonResponse({'error': f"Company webhook '{slug}' is disabled."}, status=403)

    tender_reference = (payload.get('tender_reference') or '').strip()
    legacy_reference = (payload.get('cargo_reference') or '').strip()
    trans_reference = (payload.get('cargo_name') or legacy_reference).strip()
    tender = None
    if tender_reference:
        tender = Tender.objects.filter(
            Q(reference=tender_reference) | Q(trans_reference=tender_reference)
        ).first()
        if tender is None:
            submission = TenderSubmission.objects.filter(trans_reference=tender_reference)\
                .select_related('tender').first()
            if submission is not None:
                tender = submission.tender
    if tender is None and legacy_reference:
        # Backward compatibility: older Odoo builds only echo a cargo reference.
        tender = Tender.objects.filter(
            Q(reference=legacy_reference) | Q(trans_reference=legacy_reference)
        ).first()
        if tender is None:
            submission = TenderSubmission.objects.filter(trans_reference=legacy_reference)\
                .select_related('tender').first()
            if submission is not None:
                tender = submission.tender

    order, created = Order.objects.update_or_create(
        odoo_company=company,
        order_id=payload['order_id'],
        defaults={
            'order_name': payload.get('order_name', '') or '',
            'state': payload.get('state', '') or '',
            'company_id': payload.get('company_id'),
            'company_name': payload.get('company_name', '') or '',
            'transporter_alias': generate_transporter_alias(),
            'date_order': parse_datetime(payload.get('date_order')),
            'amount_total': to_decimal(payload.get('amount_total')),
            'customer': (tender.customer if tender else '') or payload.get('customer', '') or '',
            'currency': payload.get('currency', '') or '',
            'trans_reference': trans_reference,
            'cargo_id': payload.get('cargo_id'),
            'user': tender.user if tender else None,
            'tender': tender,
            'raw_payload': payload,
        },
    )

    existing_aliases = dict(order.lines.values_list('line_id', 'truck_alias'))
    order.lines.all().delete()
    for line in payload.get('order_lines') or []:
        OrderLine.objects.create(
            order=order,
            line_id=line.get('line_id', 0),
            product_id=line.get('product_id'),
            product_name=line.get('product_name', '') or '',
            truck_alias=existing_aliases.get(line.get('line_id', 0)) or OrderLine.make_truck_alias(),
            quantity=to_decimal(line.get('quantity')),
            price_unit=to_decimal(line.get('price_unit')),
            commission=to_decimal(line.get('commission')),
            price_subtotal=to_decimal(line.get('price_subtotal')),
            price_total=to_decimal(line.get('price_total')),
        )

    get_or_create_transporter(order)

    notify_order_update(order)

    return JsonResponse({
        'status': 'ok',
        'created': created,
        'order_id': order.order_id,
        'trans_reference': trans_reference,
        'linked_tender': tender.tender_reference() if tender else None,
        'company_slug': company.slug,
    })


class Dashboard(LoginRequiredMixin, ListView):
    model = Tender
    template_name = 'tenders/dashboard.html'
    context_object_name = 'recent_tenders'

    def get_queryset(self):
        return Tender.objects.filter(user=self.request.user)[:5]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['company_count'] = self.request.user.companies.count()
        context['tender_count'] = Tender.objects.filter(user=self.request.user).count()
        context['order_count'] = Order.objects.filter(
            Q(user=self.request.user) | Q(user__isnull=True)
        ).count()
        return context


def _all_tenders_for(user):
    if getattr(user, 'role', None) == CustomUser.Role.ADMINISTRATOR:
        return Tender.objects.all()
    return Tender.objects.filter(user=user)


class TenderList(AgentRedirectMixin, LoginRequiredMixin, ListView):
    model = Tender
    template_name = 'tenders/tender_list.html'
    context_object_name = 'tenders'
    paginate_by = 20

    def get_queryset(self):
        return _all_tenders_for(self.request.user)


class TenderCreate(TenderCreatorRequiredMixin, LoginRequiredMixin, CreateView):
    model = Tender
    form_class = TenderForm
    template_name = 'tenders/tender_form.html'
    success_url = reverse_lazy('tenders:create')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['transporter'] = _tender_submission_setting(self.request.user)
        return context

    def form_valid(self, form):
        tender = form.save(commit=False)
        tender.user = self.request.user
        tender.save()
        tender.ensure_reference()

        targets = _available_transporters()
        if not targets:
            messages.warning(
                self.request,
                'No transport company is configured yet. An administrator must add an Odoo '
                'company (Configuration > Odoo) with a base URL before tenders can be sent.',
            )
            tender.status = Tender.Status.PENDING
            tender.save()
            return redirect('tenders:create')

        outcome = _submit_tender_to_targets(tender, targets)
        succeeded, total = outcome['succeeded'], len(targets)
        if succeeded:
            messages.success(
                self.request,
                f'Tender sent to {succeeded} of {total} transport companies.'
                + (f' Reference: {tender.tender_reference()}.' if (tender.tender_reference()) else ''),
            )
            flush_pending_pushes(user=self.request.user)
        else:
            message = f'Tender could not be sent to any transport company (0 of {total}).'
            messages.error(self.request, message)
            if _has_transient_failure(tender):
                _enqueue_tender(tender, message)
        return redirect('tenders:list')


class OrderList(LoginRequiredMixin, ListView):
    model = Order
    template_name = 'tenders/order_list.html'
    context_object_name = 'orders'
    paginate_by = 20

    def get_queryset(self):
        return (
            Order.objects.filter(
                Q(user=self.request.user) | Q(user__isnull=True)
            )
            .select_related('tender')
            .order_by('tender__trans_reference', '-date_order', '-created_at')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['ws_scheme'] = 'wss' if self.request.is_secure() else 'ws'
        context['ws_host'] = self.request.get_host()
        return context


class OrderDetail(LoginRequiredMixin, DetailView):
    model = Order
    template_name = 'tenders/order_detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        return Order.objects.filter(
            Q(user=self.request.user) | Q(user__isnull=True)
        ).select_related('tender')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['ws_scheme'] = 'wss' if self.request.is_secure() else 'ws'
        context['ws_host'] = self.request.get_host()
        return context


def _config_context(request, active):
    return {
        'active_config': active,
        'config_items': [
            (reverse('tenders:config_odoo'), 'Odoo', active == 'odoo'),
            (reverse('tenders:config_selcom'), 'Selcom', active == 'selcom'),
            (reverse('tenders:config_email'), 'Email', active == 'email'),
            (reverse('tenders:config_media'), 'Files', active == 'media'),
            (reverse('tenders:config_map'), 'Map', active == 'map'),
        ],
    }


@login_required
@_admin_required
def config_odoo(request):
    raw_pk = request.POST.get('id') or request.GET.get('edit') or ''
    try:
        edit_pk = int(raw_pk)
    except ValueError:
        edit_pk = None
    delete_pk = None
    try:
        delete_pk = int(request.POST.get('delete', '') or '')
    except ValueError:
        delete_pk = None

    if request.method == 'POST' and delete_pk:
        OdooCompany.objects.filter(pk=delete_pk).delete()
        _flush_derived_caches()
        messages.success(request, 'Odoo company removed.')
        return redirect('tenders:config_odoo')

    editing = None
    if edit_pk:
        editing = OdooCompany.objects.filter(pk=edit_pk).first()

    form = OdooCompanyConfigForm(instance=editing) if editing else OdooCompanyConfigForm()
    if request.method == 'POST':
        instance = editing
        if instance is None and request.POST.get('name'):
            instance = OdooCompany()
        form = OdooCompanyConfigForm(request.POST, instance=instance) if instance else None
        if form is not None and form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'Odoo company saved.')
            return redirect('tenders:config_odoo')
    else:
        form = OdooCompanyConfigForm(instance=editing) if editing else OdooCompanyConfigForm()

    companies = OdooCompany.objects.all()
    for company in companies:
        company.webhook_url = request.build_absolute_uri(
            reverse('tenders:webhook_order_company', kwargs={'slug': company.slug})
        )
        company.orders_count = company.orders.count()

    context = _config_context(request, 'odoo')
    context['companies'] = companies
    context['form'] = form
    context['editing'] = editing
    return render(request, 'tenders/config_odoo.html', context)


@login_required
@_admin_required
def config_selcom(request):
    setting = _platform_setting()
    if request.method == 'POST':
        form = SelcomConfigForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'Selcom configuration saved.')
            return redirect('tenders:config_selcom')
    else:
        form = SelcomConfigForm(instance=setting)
    context = _config_context(request, 'selcom')
    context['form'] = form
    context['selcom_webhook_url'] = request.build_absolute_uri(reverse('tenders:webhook_selcom'))
    return render(request, 'tenders/config_selcom.html', context)


@login_required
@_admin_required
def config_email(request):
    setting = _platform_setting()
    if request.method == 'POST':
        section = request.POST.get('section', 'incoming') == 'outgoing'
        incoming_form = IncomingEmailConfigForm(
            request.POST if not section else None, instance=setting)
        outgoing_form = OutgoingEmailConfigForm(
            request.POST if section else None, instance=setting)
        form = outgoing_form if section else incoming_form
        if form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'Email configuration saved.')
            return redirect('tenders:config_email')
    else:
        incoming_form = IncomingEmailConfigForm(instance=setting)
        outgoing_form = OutgoingEmailConfigForm(instance=setting)
    context = _config_context(request, 'email')
    context['incoming_form'] = incoming_form
    context['outgoing_form'] = outgoing_form
    return render(request, 'tenders/config_email.html', context)


@login_required
@_admin_required
def config_media(request):
    setting = _platform_setting()
    if request.method == 'POST':
        form = MediaConfigForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'File storage configuration saved.')
            return redirect('tenders:config_media')
    else:
        form = MediaConfigForm(instance=setting)
    context = _config_context(request, 'media')
    context['form'] = form
    context['media_root'] = str(settings.MEDIA_ROOT)
    context['s3'] = {
        'configured': bool(settings.AWS_STORAGE_BUCKET_NAME.strip()),
        'bucket': settings.AWS_STORAGE_BUCKET_NAME,
        'region': settings.AWS_S3_REGION_NAME,
        'endpoint': settings.AWS_S3_ENDPOINT_URL,
        'custom_domain': settings.AWS_S3_CUSTOM_DOMAIN,
        'access_key_set': bool(settings.AWS_ACCESS_KEY_ID.strip()),
        'secret_key_set': bool(settings.AWS_SECRET_ACCESS_KEY.strip()),
    }
    return render(request, 'tenders/config_media.html', context)


@login_required
@_admin_required
def config_map(request):
    setting = _platform_setting()
    if request.method == 'POST':
        form = MapConfigForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'Map configuration saved. Tracking maps will use the new tiles.')
            return redirect('tenders:config_map')
    else:
        form = MapConfigForm(instance=setting)
    context = _config_context(request, 'map')
    context['form'] = form
    context['map_default_url'] = setting.map_resolved_tile_url()
    context['map_default_attribution'] = setting.map_resolved_attribution()
    context['needs_key'] = setting.map_provider in ApiSetting.MAP_PROVIDERS_NEEDING_KEY
    return render(request, 'tenders/config_map.html', context)


# ---------------------------------------------------------------------------
# JSON API endpoints (used by the Vue front end)
# ---------------------------------------------------------------------------

def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return {}


def _tender_dict(tender):
    companies = [
        {'id': s.odoo_company_id, 'name': s.odoo_company.name, 'base_url': s.odoo_company.base_url}
        for s in tender.submissions.select_related('odoo_company').order_by('created_at')
    ]
    return {
        'id': tender.id,
        'customer': tender.customer,
        'route': f'{tender.route_loading} -> {tender.route_delivery}',
        'cargo_type': tender.get_cargo_type_display(),
        'truck_type': tender.get_truck_type_display(),
        'weight': tender.weight,
        'number_of_trucks': tender.number_of_trucks,
        'distance_km': tender.distance_km,
        'cargo_date': tender.cargo_date.isoformat() if tender.cargo_date else None,
        'status': tender.status,
        'status_label': tender.get_status_display(),
        'reference': tender.reference,
        'trans_reference': tender.trans_reference,
        'response_code': tender.response_code,
        'payment_terms': tender.payment_terms.name if tender.payment_terms_id else '',
        'payment_term_id': tender.payment_terms_id,
        'odoo_companies': companies,
        'created_at': tender.created_at.isoformat() if tender.created_at else None,
    }


def _order_invoice(order):
    try:
        return order.invoice
    except ObjectDoesNotExist:
        return None


def _visible_orders_q(request):
    if request.user.role == CustomUser.Role.ADMINISTRATOR:
        return Q()
    return Q(user=request.user) | Q(user__isnull=True)


def _order_dict(order, include_lines=False):
    if hasattr(order, '_line_count'):
        line_count = order._line_count or 0
    else:
        line_count = order.lines.count()
    if hasattr(order, '_awarded_line_count'):
        awarded_count = order._awarded_line_count or 0
    else:
        awarded_count = order.awarded_lines_count
    if hasattr(order, '_awarded_amount_total'):
        awarded_amount = order._awarded_amount_total or 0
    else:
        awarded_amount = order.awarded_amount
    invoice = _order_invoice(order)
    data = {
        'id': order.pk,
        'order_id': order.order_id,
        'order_name': order.order_name,
        'state': order.state or '',
        'customer': order.customer,
        'company_name': order.company_name,
        'transporter_alias': order.transporter_alias,
        'company_id': order.company_id,
        'currency': order.currency,
        'amount_total': str(order.amount_total),
        'awarded_amount': str(awarded_amount or 0),
        'awarded_lines_count': awarded_count,
        'total_lines_count': line_count,
        'trans_reference': order.trans_reference,
        'cargo_id': order.cargo_id,
        'date_order': order.date_order.isoformat() if order.date_order else None,
        'created_at': order.created_at.isoformat() if order.created_at else None,
        'updated_at': order.updated_at.isoformat() if order.updated_at else None,
        'tender_ref': order.tender.tender_reference() if order.tender else None,
        'tender_route': f"{order.tender.route_loading} -> {order.tender.route_delivery}" if order.tender else '',
        'tender_loading': order.tender.route_loading if order.tender else '',
        'tender_delivery': order.tender.route_delivery if order.tender else '',
        'fully_confirmed': order.fully_confirmed,
        'partially_confirmed': order.partially_confirmed,
        'remaining_line_ids': order.remaining_line_ids,
        'removed_line_ids': order.removed_line_ids,
        'awarded_at': order.awarded_at.isoformat() if order.awarded_at else None,
        'award_message': (order.award_response_data or {}).get('message', ''),
        'invoice_id': invoice.pk if invoice else None,
        'payment_status': invoice.status if invoice else None,
        'payment_term_id': order.tender.payment_terms_id if order.tender else None,
        'payment_terms': order.tender.payment_terms.name if order.tender and order.tender.payment_terms_id else '',
    }
    if include_lines:
        data['lines'] = [
            {
                'line_id': line.line_id,
                'product_id': line.product_id,
                'truck_alias': line.truck_alias or f'TRK-{line.line_id}',
                'quantity': str(line.quantity),
                'price_unit': str(line.price_unit),
                'price_subtotal': str(line.price_subtotal),
                'tax': str(line.price_total - line.price_subtotal),
                'price_total': str(line.price_total),
                'awarded': line.awarded,
            }
            for line in order.lines.order_by('line_id')
        ]
    return data


def _form_meta():
    return {
        'towns': [{'value': value, 'label': label} for value, label in TOWN_CHOICES],
        'cargo_types': [{'value': c, 'label': Tender.CargoType(c).label} for c in Tender.CargoType.values],
        'truck_types': [{'value': c, 'label': Tender.TruckType(c).label} for c in Tender.TruckType.values],
        'auth_types': [{'value': c, 'label': ApiSetting.AuthType(c).label} for c in ApiSetting.AuthType.values],
    }


@login_required
def api_dashboard(request):
    def loader():
        recent = Tender.objects.filter(user=request.user)[:5]
        return {
            'ok': True,
            'email': request.user.email,
            'company_count': request.user.companies.count(),
            'tender_count': Tender.objects.filter(user=request.user).count(),
            'order_count': Order.objects.filter(
                Q(user=request.user) | Q(user__isnull=True)
            ).count(),
            'recent_tenders': [_tender_dict(t) for t in recent],
        }

    return JsonResponse(_cached(_cache_key('dash', request.user.pk), 60, loader))


@login_required
def _page_param(request):
    try:
        return max(int(request.GET.get('page', 1)), 1)
    except (TypeError, ValueError):
        return 1


def _page_meta(page, pages, per_page, total):
    return {
        'page': page,
        'pages': pages,
        'per_page': per_page,
        'total': total,
        'has_next': page < pages,
        'has_prev': page > 1,
    }


def _cache_key(prefix, *parts):
    return 'api:' + prefix + (':' + ':'.join(str(p) for p in parts) if parts else '')


def _cached(key, timeout, loader):
    try:
        data = cache.get(key)
        if data is not None:
            return data
    except Exception as exc:
        logger.warning('Cache read failed for %s: %s', key, exc)
    data = loader()
    try:
        cache.set(key, data, timeout)
    except Exception as exc:
        logger.warning('Cache write failed for %s: %s', key, exc)
    return data


def _cached_json_bytes(key, timeout, loader):
    try:
        raw = cache.get(key)
        if raw is not None:
            return HttpResponse(raw, content_type='application/json')
    except Exception as exc:
        logger.warning('Cache read failed for %s: %s', key, exc)
    data = loader()
    try:
        cache.set(key, json.dumps(data, separators=(',', ':')), timeout)
    except Exception as exc:
        logger.warning('Cache write failed for %s: %s', key, exc)
    return JsonResponse(data)


def _flush_derived_caches():
    try:
        cache.delete(MAP_CONFIG_CACHE_KEY)
        if hasattr(cache, 'delete_pattern'):
            cache.delete_pattern('api:dash:*')
            cache.delete_pattern('api:tl:*')
            cache.delete_pattern('api:ol:*')
            cache.delete_pattern('api:trk:*')
    except Exception as exc:
        logger.warning('Derived-cache flush failed: %s', exc)


@login_required
def api_tender_list(request):
    page = _page_param(request)

    def loader():
        per_page = 15
        qs = _all_tenders_for(request.user)
        total = qs.count()
        pages = max((total + per_page - 1) // per_page, 1)
        p = min(page, pages)
        tenders = qs.order_by('-created_at')[(p - 1) * per_page: p * per_page]
        data = _page_meta(p, pages, per_page, total)
        data.update({'ok': True, 'tenders': [_tender_dict(t) for t in tenders]})
        return data

    return JsonResponse(_cached(_cache_key('tl', request.user.pk, page), 30, loader))


@login_required
def api_form_meta(request):
    meta = _form_meta()
    meta['payment_terms'] = [_payment_term_dict(t) for t in request.user.payment_terms.order_by('-created_at')]
    meta['transporters'] = [
        {'id': c.pk, 'name': c.name, 'base_url': c.base_url}
        for c in _available_transporters()
    ]
    setting = _tender_submission_setting(request.user)
    meta['transporter'] = {
        'name': setting.name if setting else None,
        'slug': setting.slug if setting else None,
        'base_url': setting.base_url if setting else None,
        'id': setting.id if setting else None,
    }
    return JsonResponse({'ok': True, 'meta': meta})


@login_required
def api_tender_create(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    form = TenderForm(_json_body(request), user=request.user)
    if not form.is_valid():
        return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': form.errors})

    tender = form.save(commit=False)
    tender.user = request.user
    tender.save()
    tender.ensure_reference()
    _flush_derived_caches()

    targets = _available_transporters()
    if not targets:
        result = {
            'ok': True,
            'message': (
                'Tender saved locally. No transport company is configured yet — an administrator '
                'must add an Odoo company (Configuration > Odoo) with a base URL before tenders can be sent.'
            ),
            'tender': _tender_dict(tender),
            'needs_settings': True,
        }
        return JsonResponse(result)

    _submit_tender_to_targets(tender, targets)
    succeeded = tender.submissions.filter(success=True).count()
    total = len(targets)

    result = {'ok': True, 'tender': _tender_dict(tender)}
    if succeeded:
        result['message'] = (
            f'Tender sent to {succeeded} of {total} transport companies.'
            + (f' Reference: {tender.tender_reference()}.' if (tender.tender_reference()) else '')
        )
        # The API is reachable right now, so try to flush any queued submissions.
        flush_pending_pushes(user=request.user)
    else:
        failed_sub = tender.submissions.filter(success=False).order_by('-created_at').first()
        base_message = f'Tender could not be sent to any transport company (0 of {total}).'
        if failed_sub is not None:
            base_message += ' ' + _submit_failure_message('Tender submission', failed_sub.status_code, failed_sub.response_body)
        result['message'] = base_message
        for sub in tender.submissions.filter(success=False):
            _record_diagnostic(
                request, 'tender.submit',
                'Tender submission failed: ' + _submit_failure_message('HTTP error', sub.status_code, sub.response_body),
                status_code=sub.status_code, method='POST',
                path=sub.odoo_company.endpoint_url(),
                detail={'response': sub.response_body, 'transport_company': sub.odoo_company_id},
            )

    if _has_transient_failure(tender):
        _enqueue_tender(tender, result['message'])
        result['queued'] = True
        result['message'] += (
            ' The submission has been queued and will be sent automatically when the '
            'Odoo instance is reachable again.'
        )
    return JsonResponse(result)


@login_required
def api_order_list(request):
    page = _page_param(request)
    role = request.user.role
    scope = 'all' if role == CustomUser.Role.ADMINISTRATOR else request.user.pk

    def order_key(ref, trans_ref):
        return (ref or trans_ref or '').strip() or None

    def loader():
        per_page = 15
        base = Order.objects.filter(_visible_orders_q(request))
        total = base.count()
        ordered = base.order_by('-date_order', '-created_at')
        if total == 0:
            data = _page_meta(1, 1, per_page, 0)
            data.update({'ok': True, 'groups': [], 'total': 0})
            return data

        rows = list(ordered.values_list('pk', 'tender__reference', 'tender__trans_reference', 'trans_reference'))
        ids = [r[0] for r in rows]
        keys = [order_key(r[1], r[2]) if (r[1] or r[2]) else order_key(None, r[3]) for r in rows]

        first_index = {}
        last_index = {}
        for i, key in enumerate(keys):
            if key is not None:
                first_index.setdefault(key, i)
                last_index[key] = i
            else:
                first_index.setdefault(key, i)
                last_index[key] = i

        bounds = [0]
        start = 0
        n = len(ids)
        while start < n:
            end = min(start + per_page, n)
            while True:
                spill = None
                for i in range(start, end):
                    k = keys[i]
                    last_i = last_index[k]
                    if last_i >= end:
                        spill = last_i + 1
                        break
                if spill is None:
                    break
                end = spill
            bounds.append(end)
            start = end

        pages = max(len(bounds) - 1, 1)
        p = min(page, pages)
        page_ids = ids[bounds[p - 1]:bounds[p]]
        orders = (
            base.filter(pk__in=page_ids)
            .prefetch_related('tender', 'invoice')
            .annotate(
                _line_count=Count('lines'),
                _awarded_line_count=Count('lines', filter=Q(lines__awarded=True)),
                _awarded_amount_total=Sum('lines__price_total', filter=Q(lines__awarded=True)),
            )
            .order_by('-date_order', '-created_at')
        )
        grouped = {}
        for o in orders:
            key = order_key(o.tender.reference, o.tender.trans_reference) if o.tender_id else order_key(None, o.trans_reference)
            grouped.setdefault(key, []).append(o)
        groups = [{
            'grouper': key or '',
            'label': key or 'Unlinked orders',
            'orders': [_order_dict(o) for o in items],
        } for key, items in grouped.items()]
        data = _page_meta(p, pages, per_page, total)
        data.update({'ok': True, 'groups': groups, 'total': len(page_ids)})
        return data

    return JsonResponse(_cached(_cache_key('ol', scope, page), 5, loader))


@login_required
def api_order_detail(request, pk):
    order = (
        Order.objects.filter(_visible_orders_q(request))
        .select_related('tender')
        .filter(pk=pk)
        .first()
    )
    if order is None:
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    return JsonResponse({'ok': True, 'order': _order_dict(order, include_lines=True)})


@login_required
def api_order_award(request, pk):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    order = (
        Order.objects.filter(_visible_orders_q(request))
        .filter(pk=pk)
        .first()
    )
    if order is None:
        return JsonResponse({'ok': False, 'error': 'Order not found.'})

    setting = _order_endpoint(order)
    if setting is None:
        return JsonResponse({
            'ok': False,
            'error': (
                'This order has no Odoo company (transporter) with a base URL, so confirmations cannot be sent. '
                'An administrator must update the transporter that posted the order.'
            ),
            'redirect': reverse('tenders:config_odoo'),
        })

    data = _json_body(request)
    try:
        line_ids = [int(x) for x in (data.get('line_ids') or []) if str(x).isdigit()]
    except (TypeError, ValueError):
        line_ids = []
    is_partial = bool(data.get('partial')) or bool(line_ids)

    result = perform_award(order, setting, line_ids, is_partial)
    if not result['ok']:
        _record_diagnostic(
            request, 'order.award', result['message'], status_code=result.get('status_code'), method='POST',
            path=result.get('url', ''),
            detail={'order_id': order.order_id, 'response': result.get('response', '')},
        )
        return JsonResponse({'ok': False, 'error': result['message']})
    return JsonResponse({
        'ok': True,
        'message': result['message'],
        'order': _order_dict(result['order'], include_lines=True),
    })


@login_required
def api_order_pay(request, pk):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    order = Order.objects.filter(pk=pk).first()
    if not _payment_order_in_scope(request, order):
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    invoice = get_or_create_invoice(order)
    return _confirm_invoice_paid(invoice)


def _truck_quota_exceeded(order, extra=0):
    """Return an error message when a tender's awarded lines exceed its truck quota.

    A tender needs a fixed number of trucks; each awarded order line represents
    one quoted truck. When more lines are awarded across the whole tender than
    the number of trucks needed, payment for any of its orders is blocked (and
    confirmations adding ``extra`` lines are refused up front: the total number
    of awarded order lines can never exceed the number of trucks in the tender).
    Returns None when the quota is fine (or there is nothing to compare).
    """
    tender = order.tender if order is not None else None
    if tender is None or not tender.number_of_trucks:
        return None
    awarded = OrderLine.objects.filter(order__tender=tender, awarded=True).count()
    if awarded + extra > tender.number_of_trucks:
        if extra:
            return (
                f'Order confirmation not allowed: this tender needs {tender.number_of_trucks} truck(s), '
                f'{awarded} already awarded and this confirmation adds {extra} more.'
            )
        return (
            f'Awarded trucks have exceeded the number of trucks needed '
            f'({awarded} awarded, {tender.number_of_trucks} needed).'
        )
    return None


@login_required
def api_order_checkout(request, pk):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    order = Order.objects.filter(pk=pk).first()
    if not _payment_order_in_scope(request, order):
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    quota_error = _truck_quota_exceeded(order)
    if quota_error:
        return JsonResponse({'ok': False, 'error': quota_error})
    invoice = get_or_create_invoice(order)
    return _initiate_selcom(request, invoice)


@login_required
def route_map(request):
    loading = request.GET.get('from', '')
    delivery = request.GET.get('to', '')
    tender_id = request.GET.get('tender_id')
    source = request.GET.get('source', '')
    truck = request.GET.get('truck', '')

    truck_index = 0
    try:
        truck_index = int(truck) if truck else 0
    except (TypeError, ValueError):
        truck_index = 0

    truck_label = ''
    truck_lat = ''
    truck_lng = ''
    if truck_index and tender_id:
        tender = Tender.objects.filter(pk=tender_id).first()
        if tender is not None:
            truck_label = f"{tender.tender_reference() or f'T{tender.pk}'} \u00b7 T{truck_index}"
            origin = Town.objects.filter(name=tender.route_loading).first()
            dest = Town.objects.filter(name=tender.route_delivery).first()
            if origin is not None and dest is not None:
                route_points, route_m = get_route(origin, dest)
                route_km = route_m / 1000.0
                if not route_km:
                    route_km = float(tender.distance_km or 0)
                if route_km:
                    sim_duration = max((route_km / SIM_SPEED_KMH) * 3600 / SIM_ACCELERATION, 3.0)
                    route_distances = _route_arrays(route_points)
                    stagger = (truck_index - 1) * 120
                    elapsed = max(0.0, (timezone.now() - tender.created_at).total_seconds() - stagger)
                    progress = min(1.0, elapsed / sim_duration)
                    lat, lng = _position_at(route_points, route_distances, progress)
                    truck_lat = str(round(lat, 6))
                    truck_lng = str(round(lng, 6))

    context = {
        'loading': loading,
        'delivery': delivery,
        'tender_id': tender_id,
        'source': source,
        'truck_label': truck_label,
        'truck_lat': truck_lat,
        'truck_lng': truck_lng,
    }
    return render(request, 'tenders/route_map.html', context)


@login_required
def api_towns(request):
    towns = Town.objects.all().values('name', 'country', 'point')
    data = [
        {
            'name': town['name'],
            'country': town['country'],
            'lat': town['point'].y,
            'lng': town['point'].x,
        }
        for town in towns
    ]
    return JsonResponse({'ok': True, 'towns': data})


@login_required
def api_town_route(request):
    loading = request.GET.get('from', '')
    delivery = request.GET.get('to', '')
    if not loading or not delivery:
        return JsonResponse({'ok': False, 'error': 'Both "from" and "to" parameters are required.'}, status=400)

    try:
        origin = Town.objects.get(name=loading)
        dest = Town.objects.get(name=delivery)
    except Town.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Town not found.'}, status=404)

    return JsonResponse({
        'ok': True,
        'origin': {'name': origin.name, 'lat': float(origin.lat), 'lng': float(origin.lng)},
        'destination': {'name': dest.name, 'lat': float(dest.lat), 'lng': float(dest.lng)},
    })

SIM_SPEED_KMH = 55.0
SIM_ACCELERATION = 240.0

_route_cache = {}


def _haversine_m(lat1, lng1, lat2, lng2):
    radius = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _osrm_fetch(origin, dest):
    url = (
        'https://router.project-osrm.org/route/v1/driving/'
        f'{origin.lng},{origin.lat};{dest.lng},{dest.lat}'
        '?overview=full&geometries=geojson&alternatives=false'
    )
    response = http.get(url, timeout=1.5)
    response.raise_for_status()
    route = response.json()['routes'][0]
    return [[lat, lng] for lng, lat in route['geometry']['coordinates']], float(route['distance'])


def get_route(origin, dest):
    key = (origin.name, dest.name)
    cached = _route_cache.get(key)
    if cached is not None:
        return cached
    redis_key = 'route:' + origin.name + '\u2192' + dest.name
    try:
        cached = cache.get(redis_key)
        if cached is not None:
            _route_cache[key] = cached
            return cached
    except Exception:
        pass
    points = [[origin.lat, origin.lng], [dest.lat, dest.lng]]
    distance = _haversine_m(origin.lat, origin.lng, dest.lat, dest.lng)
    try:
        points, distance = _osrm_fetch(origin, dest)
    except Exception:
        logger.warning('OSRM route lookup failed for %s → %s; falling back to straight line', origin.name, dest.name)
    result = (points, distance)
    _route_cache[key] = result
    try:
        cache.set(redis_key, result, 86400)
    except Exception:
        pass
    return result


def _route_arrays(points):
    distances = [0.0]
    for i in range(1, len(points)):
        distances.append(distances[-1] + _haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
    return distances


def _position_at(points, distances, progress):
    total = distances[-1]
    if total <= 0 or not points:
        return points[0] if points else [0, 0]
    target = progress * total
    for i in range(1, len(points)):
        if distances[i] >= target:
            seg_len = distances[i] - distances[i - 1]
            frac = (target - distances[i - 1]) / seg_len if seg_len else 0
            lat = points[i - 1][0] + (points[i][0] - points[i - 1][0]) * frac
            lng = points[i - 1][1] + (points[i][1] - points[i - 1][1]) * frac
            return [round(lat, 6), round(lng, 6)]
    return points[-1]


def _is_awarded_tender(tender):
    return (
        tender.status == Tender.Status.SUCCESS
        or tender.orders.filter(lines__awarded=True).exists()
    )


@login_required
def tracker(request):
    return render(request, 'tenders/tracker.html')


@login_required
def api_tracker(request):
    role = request.user.role
    scope = 'all' if role == CustomUser.Role.ADMINISTRATOR else request.user.pk

    def loader():
        now = timezone.now()
        towns_by_name = {t.name: t for t in Town.objects.all()}
        awarded_orders = list(
            Order.objects.filter(_visible_orders_q(request), lines__awarded=True)
            .select_related('tender')
            .distinct()
            .order_by('-created_at')
            .annotate(
                _line_count=Count('lines', distinct=True),
                _awarded_line_count=Count(
                    'lines', filter=Q(lines__awarded=True), distinct=True
                ),
                _awarded_amount_total=Sum(
                    'lines__price_total', filter=Q(lines__awarded=True)
                ),
            )
        )
        tenders = []
        seen = set()
        for order in awarded_orders:
            tender = order.tender
            if tender is None or tender.pk in seen:
                continue
            seen.add(tender.pk)
            tenders.append(tender)
        pairs = {}
        for tender in tenders:
            origin = towns_by_name.get(tender.route_loading)
            dest = towns_by_name.get(tender.route_delivery)
            if origin is not None and dest is not None:
                pairs.setdefault((origin.name, dest.name), (origin, dest))
        resolved = {}
        if pairs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
                for names, result in ex.map(
                    lambda item: (item[0], get_route(*item[1])), pairs.items()
                ):
                    resolved[names] = result

        groups = {}
        for order in awarded_orders:
            tender = order.tender
            if tender is None:
                continue
            reference = tender.tender_reference() or f'T{tender.pk}'
            group = groups.setdefault(reference, {
                'key': reference,
                'tender_ref': tender.tender_reference() or '',
                'customer': tender.customer,
                'route': f'{tender.route_loading} \u2192 {tender.route_delivery}',
                'orders': [],
                'origin': None,
                'destination': None,
                'distance_km': 0,
                'trucks': [],
            })
            group['orders'].append({
                'id': order.pk,
                'order_id': order.order_id,
                'order_name': order.order_name,
                'company_name': order.company_name,
                'trans_reference': order.trans_reference,
                'state': order.state or '',
                'awarded_amount': str(order._awarded_amount_total or 0),
                'awarded_lines_count': order._awarded_line_count,
                'total_lines_count': order._line_count,
                'fully_confirmed': order.fully_confirmed,
                'partially_confirmed': order.partially_confirmed,
            })
            route = resolved.get((tender.route_loading, tender.route_delivery))
            if route is not None:
                origin = towns_by_name[tender.route_loading]
                dest = towns_by_name[tender.route_delivery]
                route_points, route_m = route
                route_km = route_m / 1000.0
                if not route_km:
                    route_km = float(tender.distance_km or 0)
                sim_duration = max((route_km / SIM_SPEED_KMH) * 3600 / SIM_ACCELERATION, 3.0)
                route_distances = _route_arrays(route_points)
                truck_count = max(tender.number_of_trucks or 1, 1)
                trucks = []
                for i in range(truck_count):
                    stagger = i * 120
                    elapsed = max(0.0, (now - tender.created_at).total_seconds() - stagger)
                    progress = min(1.0, elapsed / sim_duration)
                    lat, lng = _position_at(route_points, route_distances, progress)
                    trucks.append({
                        'label': f"{tender.tender_reference() or f'T{tender.pk}'} · T{i + 1}",
                        'lat': lat,
                        'lng': lng,
                        'status': 'Delivered' if progress >= 1.0 else 'En route',
                        'progress': round(progress, 4),
                    })
                group['origin'] = {'name': origin.name, 'lat': origin.lat, 'lng': origin.lng}
                group['destination'] = {'name': dest.name, 'lat': dest.lat, 'lng': dest.lng}
                group['route'] = route_points
                group['distance_km'] = round(route_km, 1)
                group['trucks'] = trucks
        return {'ok': True, 'groups': list(groups.values()), 'now': now.isoformat()}

    return _cached_json_bytes(_cache_key('trk', scope), 15, loader)


def _invoice_lines(order):
    return [
        {
            'line_id': line.line_id,
            'product_name': line.product_name,
            'quantity': str(line.quantity),
            'price_unit': str(line.price_unit),
            'price_subtotal': str(line.price_subtotal),
            'tax': str(line.price_total - line.price_subtotal),
            'price_total': str(line.price_total),
        }
        for line in order.lines.filter(awarded=True).order_by('line_id')
    ]


def _invoice_dict(invoice):
    order = invoice.order
    tender = order.tender
    lines = _invoice_lines(order)
    return {
        'id': invoice.pk,
        'order_pk': order.pk,
        'number': invoice.number,
        'status': invoice.status,
        'amount_total': str(invoice.amount_total),
        'deposited_amount': str(invoice.deposited_amount),
        'payment_terms': tender.payment_terms.name if tender and tender.payment_terms_id else '',
        'payment_term_id': tender.payment_terms_id if tender else None,
        'currency': invoice.currency,
        'created_at': invoice.created_at.isoformat(),
        'order_id': order.order_id,
        'order_name': order.order_name or f'#{order.order_id}',
        'transporter': invoice.transporter.company_name if invoice.transporter else (order.company_name or ''),
        'customer': order.customer or '',
        'trans_reference': order.trans_reference,
        'cargo_id': order.cargo_id,
        'route': f'{tender.route_loading} \u2192 {tender.route_delivery}' if tender else '',
        'tender_loading': tender.route_loading if tender else '',
        'tender_delivery': tender.route_delivery if tender else '',
        'tender_reference': tender.tender_reference() if tender else '',
        'tender_id': tender.pk if tender else None,
        'order_user': order.user.email if order.user else '',
        'selcom_reference': invoice.selcom_reference,
        'selcom_order_token': invoice.selcom_order_token,
        'selcom_pay_link': invoice.selcom_pay_link,
        'selcom_status': invoice.selcom_status,
        'selcom_updated_at': invoice.selcom_updated_at.isoformat() if invoice.selcom_updated_at else None,
        'lines': lines,
        'trucks': [l['product_name'] for l in lines],
    }


def _synthetic_invoice_dict(order):
    tender = order.tender
    lines = _invoice_lines(order)
    awarded_amount = order.awarded_amount
    if awarded_amount is None:
        awarded_amount = order.amount_total or 0
    return {
        'id': None,
        'order_pk': order.pk,
        'number': f'INV-{order.order_id}',
        'status': Invoice.Status.PENDING,
        'amount_total': str(awarded_amount),
        'deposited_amount': '0.00',
        'payment_terms': tender.payment_terms.name if tender and tender.payment_terms_id else '',
        'payment_term_id': tender.payment_terms_id if tender else None,
        'currency': order.currency or 'TZS',
        'created_at': None,
        'order_id': order.order_id,
        'order_name': order.order_name or f'#{order.order_id}',
        'transporter': order.company_name or '',
        'customer': order.customer or '',
        'trans_reference': order.trans_reference,
        'cargo_id': order.cargo_id,
        'route': f'{tender.route_loading} \u2192 {tender.route_delivery}' if tender else '',
        'tender_loading': tender.route_loading if tender else '',
        'tender_delivery': tender.route_delivery if tender else '',
        'tender_reference': tender.tender_reference() if tender else '',
        'tender_id': tender.pk if tender else None,
        'order_user': order.user.email if order.user else '',
        'selcom_reference': None,
        'selcom_order_token': None,
        'selcom_pay_link': None,
        'selcom_status': None,
        'selcom_updated_at': None,
        'lines': lines,
        'trucks': [l['product_name'] for l in lines],
    }


def _payment_term_dict(term):
    return {
        'id': term.pk,
        'name': term.name,
        'description': term.description,
        'is_active': term.is_active,
        'items': [
            {'id': item.pk, 'percent': item.percent, 'text': item.text}
            for item in term.items.order_by('sort_order', 'created_at')
        ],
        'created_at': term.created_at.isoformat() if term.created_at else None,
    }


def _escrow_dict(escrow):
    invoices = list(escrow.invoices.select_related('order', 'transporter').order_by('created_at'))
    order = invoices[0].order if invoices else None
    tender = escrow.tender
    tender_reference = tender.tender_reference() if tender else ''
    if not tender_reference and order and order.trans_reference:
        tender_reference = order.trans_reference
    transporter_names = []
    seen = set()
    for inv in invoices:
        t = inv.transporter
        name = (t.company_name or t.alias).strip() if t else ''
        if not name:
            name = (inv.order.company_name or '').strip() if inv.order_id else ''
        if name and name not in seen:
            seen.add(name)
            transporter_names.append(name)
    tender_customer = tender.customer if tender else ''
    pending_tokens = [i.selcom_order_token for i in invoices if i.selcom_order_token]
    trucks = []
    for inv in invoices:
        for line in inv.order.lines.filter(awarded=True).order_by('line_id'):
            if line.product_name and line.product_name not in trucks:
                trucks.append(line.product_name)
    return {
        'id': escrow.pk,
        'detail_url': reverse('tenders:admin_escrow_detail', args=[escrow.pk]),
        'virtual_account': escrow.virtual_account or '',
        'customer': tender_customer or '-',
        'transporter': ', '.join(transporter_names) or '-',
        'transporter_names': transporter_names,
        'amount': str(escrow.amount),
        'currency': invoices[0].currency if invoices else 'TZS',
        'payment_terms': escrow.payment_terms.name if escrow.payment_terms else '',
        'payment_terms_description': escrow.payment_terms.description if escrow.payment_terms else '',
        'created_at': escrow.created_at.isoformat() if escrow.created_at else None,
        'tender_reference': tender_reference or '',
        'invoice_numbers': [i.number for i in invoices],
        'invoice_number': ', '.join(i.number for i in invoices),
        'bank': escrow.bank or 'Selcom',
        'deposited_amount': str(escrow.deposited_amount),
        'status': escrow.status,
        'status_label': escrow.get_status_display(),
        'selcom_order_token': pending_tokens[0] if pending_tokens else '',
        'selcom_pay_link': invoices[0].selcom_pay_link if invoices else '',
        'selcom_status': invoices[0].selcom_status if invoices else '',
        'trucks': trucks,
    }


def _company_for_order(order):
    if order.customer:
        company = Company.objects.filter(name=order.customer).first()
        if company is not None:
            return company
    if order.user_id:
        return order.user.companies.first()
    return None


def _invoice_payload(invoice):
    order = invoice.order
    company = _company_for_order(order)
    return {
        'order_id': order.order_id,
        'cargo_name': (order.tender.tender_reference() if order.tender else '') or order.trans_reference or '',
        'tender_reference': order.tender.tender_reference() if order.tender else '',
        'customer_name': (order.customer or '').strip(),
        'tax_id': company.tin if company else '',
        'country': company.country if company else '',
    }


@login_required
def invoice_list(request):
    return render(request, 'tenders/invoice_list.html', {'active_tab': 'invoices'})


@login_required
def api_invoices(request):
    order_q = _visible_orders_q(request)
    awarded_orders = (
        Order.objects.filter(order_q, lines__awarded=True)
        .select_related('tender')
        .distinct()
    )
    invoices = (
        Invoice.objects.filter(status=Invoice.Status.PAID)
        .filter(order__in=awarded_orders)
        .select_related('order__tender', 'transporter')
        .order_by('-order__awarded_at', '-created_at')
    )
    rows = [_invoice_dict(inv) for inv in invoices]
    return JsonResponse({
        'ok': True,
        'selcom_enabled': _platform_setting().selcom_enabled,
        'invoices': rows,
    })


def _external_invoice_number(parsed):
    if not isinstance(parsed, dict):
        return ''
    data = parsed.get('data')
    if isinstance(data, dict):
        return str(data.get('name', '') or '')
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get('name'):
                return str(item['name'])
    return ''


@login_required
@require_POST
def api_invoice_paid(request, pk):
    invoice = Invoice.objects.select_related('order__tender', 'transporter').filter(pk=pk).first()
    if invoice is None or not _payment_order_in_scope(request, invoice.order):
        return JsonResponse({'ok': False, 'error': 'Invoice not found.'}, status=404)
    quota_error = _truck_quota_exceeded(invoice.order)
    if quota_error:
        return JsonResponse({'ok': False, 'error': quota_error})
    return _confirm_invoice_paid(invoice)


def _confirm_invoice_paid(invoice):
    order = invoice.order
    setting = _order_endpoint(order)
    if setting is None:
        return JsonResponse({'ok': False, 'error': 'This order has no Odoo company (transporter) with a base URL, so the invoice cannot be confirmed.'})

    payload = _invoice_payload(invoice)
    url = setting.order_invoice_url()
    status_code, body, _ok = submit_confirmation(setting, url, payload)

    is_ok = status_code is not None and 200 <= status_code < 300
    parsed = {}
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, TypeError):
        pass
    if isinstance(parsed, dict) and parsed.get('status') == 'success':
        is_ok = True

    if not is_ok:
        return JsonResponse({
            'ok': False,
            'error': f'Invoice confirmation to {url} failed (HTTP {status_code}). Response: {body[:300]}',
        })

    invoice.status = Invoice.Status.PAID
    local_number = invoice.number
    update_fields = ['status']
    external_name = _external_invoice_number(parsed)
    if external_name and not Invoice.objects.filter(number=external_name).exclude(pk=invoice.pk).exists():
        invoice.number = str(external_name)[:50]
        update_fields.append('number')
    try:
        invoice.save(update_fields=update_fields)
    except IntegrityError:
        if 'number' not in update_fields:
            raise
        # Another invoice already holds this external number (two orders paid
        # against the same external invoice). Keep our locally issued number.
        invoice.number = local_number
        invoice.save(update_fields=['status'])
    _flush_derived_caches()
    return JsonResponse({
        'ok': True,
        'message': 'Invoice confirmed as paid.',
        'invoice': _invoice_dict(invoice),
        'submitted': payload,
    })


# ---------------------------------------------------------------------------
# Payment terms (user-managed library)
# ---------------------------------------------------------------------------

@login_required
def payment_terms_page(request):
    return render(request, 'tenders/payment_terms.html', {'active_tab': 'payment_terms'})


@login_required
def api_payment_terms(request):
    terms = request.user.payment_terms.order_by('-created_at')
    return JsonResponse({'ok': True, 'payment_terms': [_payment_term_dict(t) for t in terms]})


@login_required
@require_POST
def api_payment_term_create(request):
    body = _json_body(request) or {}
    form = PaymentTermForm(body)
    if not form.is_valid():
        return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': form.errors})
    balance_error = _check_percent_balance(raw_items=body.get('items') or [])
    if balance_error:
        return JsonResponse({'ok': False, 'error': balance_error})
    term = form.save(commit=False)
    term.user = request.user
    term.is_active = True
    term.save()
    _create_payment_term_items(term, body.get('items') or [])
    return JsonResponse({
        'ok': True,
        'message': 'Payment term created.',
        'payment_term': _payment_term_dict(term),
    })


def _parse_percent(raw):
    """Parse a term-detail percentage (0-100). Returns None when absent or invalid."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(Decimal(str(raw).strip()))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if not 0 <= value <= 100:
        return None
    return value


def _check_percent_balance(raw_items=None, existing_items=None):
    """
    When every item carries a percentage, the total must equal exactly 100.
    Returns None when valid (or no percentages are used at all), else an error string.
    """
    total = 0
    any_pct = False
    any_missing = False

    def _tally(pct):
        nonlocal total, any_pct, any_missing
        if pct is not None:
            total += pct
            any_pct = True
        else:
            any_missing = True

    for raw in (raw_items or []):
        if isinstance(raw, dict) and str(raw.get('text') or '').strip():
            _tally(_parse_percent(raw.get('percent')))
    for item in (existing_items or []):
        pct = item.get('percent') if isinstance(item, dict) else item.percent
        _tally(pct)
    if not any_pct or any_missing:
        return None
    if total > 100:
        return 'Aggregate payment term percentage cannot exceed 100%.'
    if total < 100:
        return 'Aggregate payment term percentage must equal exactly 100%.'
    return None


def _create_payment_term_items(term, raw_items):
    sort = 0
    if isinstance(raw_items, (list, tuple)):
        for raw in raw_items:
            text = ''
            percent = None
            if isinstance(raw, dict):
                text = str(raw.get('text') or '').strip()
                percent = _parse_percent(raw.get('percent'))
            elif isinstance(raw, str):
                text = raw.strip()
            if not text:
                continue
            term.items.create(text=text[:300], percent=percent, sort_order=sort)
            sort += 1


@login_required
@require_POST
def api_payment_term_add_item(request, pk):
    term = request.user.payment_terms.filter(pk=pk).first()
    if term is None:
        return JsonResponse({'ok': False, 'error': 'Payment term not found.'}, status=404)
    body = _json_body(request) or {}
    text = str(body.get('text') or '').strip()
    if not text:
        return JsonResponse({'ok': False, 'error': 'Term detail text is required.'})
    percent = _parse_percent(body.get('percent'))
    if percent is not None:
        check_items = [{'percent': item.percent} for item in term.items.all()]
        check_items.append({'percent': percent})
        balance_error = _check_percent_balance(existing_items=check_items)
        if balance_error:
            return JsonResponse({'ok': False, 'error': balance_error})
    last = term.items.order_by('-sort_order').first()
    sort = (last.sort_order + 1) if last else 0
    item = term.items.create(
        text=text[:300],
        percent=percent,
        sort_order=sort,
    )
    return JsonResponse({
        'ok': True,
        'message': 'Term detail added.',
        'item': {'id': item.pk, 'percent': item.percent, 'text': item.text},
        'payment_term': _payment_term_dict(term),
    })


@login_required
@require_POST
def api_payment_term_item_delete(request, pk, item_pk):
    term = request.user.payment_terms.filter(pk=pk).first()
    if term is None:
        return JsonResponse({'ok': False, 'error': 'Payment term not found.'}, status=404)
    item = term.items.filter(pk=item_pk).first()
    if item is None:
        return JsonResponse({'ok': False, 'error': 'Term detail not found.'}, status=404)
    item.delete()
    return JsonResponse({'ok': True, 'message': 'Term detail removed.', 'payment_term': _payment_term_dict(term)})


@login_required
@require_POST
def api_payment_term_delete(request, pk):
    term = request.user.payment_terms.filter(pk=pk).first()
    if term is None:
        return JsonResponse({'ok': False, 'error': 'Payment term not found.'}, status=404)
    term.delete()
    return JsonResponse({'ok': True, 'message': 'Payment term deleted.'})


@login_required
@require_POST
def api_payment_term_toggle(request, pk):
    term = request.user.payment_terms.filter(pk=pk).first()
    if term is None:
        return JsonResponse({'ok': False, 'error': 'Payment term not found.'}, status=404)
    term.is_active = not term.is_active
    term.save(update_fields=('is_active',))
    return JsonResponse({'ok': True, 'message': 'Payment term updated.', 'payment_term': _payment_term_dict(term)})


# ---------------------------------------------------------------------------
# Escrow accounts (administrator)
# ---------------------------------------------------------------------------

@login_required
@_admin_required
def admin_escrow(request):
    return render(request, 'tenders/admin_escrow.html', {'active_tab': 'escrow'})


@login_required
@_admin_required
def api_admin_escrow(request):
    accounts = (
        EscrowAccount.objects
        .annotate(
            _total_invoices=Count('invoices'),
            _pending_invoices=Count('invoices', filter=Q(invoices__status=Invoice.Status.PENDING)),
        )
        .filter(_total_invoices__gt=0, _pending_invoices=0)
        .select_related('tender', 'user', 'payment_terms')
        .prefetch_related('invoices__order__tender', 'invoices__transporter', 'transporters')
        .order_by('-created_at')
    )
    return JsonResponse({'ok': True, 'escrow_accounts': [_escrow_dict(a) for a in accounts]})


@login_required
@_admin_required
def admin_escrow_detail(request, pk):
    escrow = get_object_or_404(
        EscrowAccount.objects.select_related('tender', 'user', 'payment_terms'),
        pk=pk,
    )
    invoices = (
        escrow.invoices
        .select_related('order', 'transporter')
        .prefetch_related('order__lines')
        .order_by('created_at')
    )
    currency = invoices[0].currency if invoices else 'TZS'

    term_items = []
    if escrow.payment_terms_id:
        items = list(escrow.payment_terms.items.order_by('sort_order', 'created_at'))
        term_items = [i for i in items if i.percent is not None]
        text_only_terms = [i for i in items if i.percent is None]
    else:
        text_only_terms = []

    breakdown = []
    for inv in invoices:
        transporter_name = ''
        if inv.transporter_id:
            transporter_name = (inv.transporter.company_name or inv.transporter.alias or '').strip()
        if not transporter_name and inv.order_id:
            transporter_name = (inv.order.company_name or '').strip()

        awarded_total = Decimal('0.00')
        unit_total = Decimal('0.00')
        commission_total = Decimal('0.00')
        for line in inv.order.lines.filter(awarded=True):
            awarded_total += line.price_total or Decimal('0.00')
            unit_total += line.price_unit or Decimal('0.00')
            commission_total += line.commission or Decimal('0.00')
        commission_total = commission_total.quantize(Decimal('0.01'))
        invoice_total = (unit_total * Decimal('1.15')).quantize(Decimal('0.01'))

        agent_name = ''
        agent_commission_rate = Decimal('0.00')
        agent_pool_share = Decimal('0.00')
        agent_vat = Decimal('0.00')
        agent_commission_amount = Decimal('0.00')
        vat_total = (awarded_total - (Decimal('1.15') * unit_total) - commission_total).quantize(Decimal('0.01'))
        if inv.transporter_id:
            first_agent = inv.transporter.agents.select_related('profile').order_by('pk').first()
            if first_agent is not None:
                agent_name = first_agent.get_full_name() or first_agent.username
                profile = getattr(first_agent, 'profile', None)
                if profile is not None:
                    agent_commission_rate = profile.agent_commission or Decimal('0.00')
                    rate = agent_commission_rate / Decimal('100')
                    agent_pool_share = (rate * commission_total).quantize(Decimal('0.01'))
                    agent_vat = (rate * vat_total).quantize(Decimal('0.01'))
                    agent_commission_amount = (agent_pool_share + agent_vat).quantize(Decimal('0.01'))
        hypax_commission = (
            (commission_total - agent_pool_share) + (vat_total - agent_vat)
        ).quantize(Decimal('0.01'))

        line_items = []
        for ti in term_items:
            amount = (Decimal(ti.percent) / Decimal('100')) * invoice_total
            line_items.append({
                'text': ti.text,
                'percent': ti.percent,
                'amount': amount.quantize(Decimal('0.01')),
            })

        breakdown.append({
            'invoice_number': inv.number,
            'transporter_name': transporter_name or '-',
            'invoice_total': invoice_total,
            'awarded_total': awarded_total,
            'unit_total': unit_total,
            'commission_total': commission_total,
            'vat_total': vat_total,
            'hypax_vat': (vat_total - agent_vat).quantize(Decimal('0.01')),
            'deposited_amount': inv.deposited_amount or Decimal('0.00'),
            'status': inv.status,
            'status_label': inv.get_status_display(),
            'line_items': line_items,
            'hypax_commission': hypax_commission,
            'agent_name': agent_name,
            'agent_commission_rate': agent_commission_rate,
            'agent_pool_share': agent_pool_share,
            'agent_vat': agent_vat,
            'agent_commission_amount': agent_commission_amount,
        })

    context = {
        'escrow': escrow,
        'escrow_data': _escrow_dict(escrow),
        'currency': currency,
        'term_items': term_items,
        'text_only_terms': text_only_terms,
        'transporter_breakdown': breakdown,
        'active_tab': 'escrow',
    }
    return render(request, 'tenders/admin_escrow_detail.html', context)


# ---------------------------------------------------------------------------
# Users overview (administrator)
# ---------------------------------------------------------------------------

def _online_user_ids():
    """User IDs with a session that has been active recently.

    A live session is one whose ``last_activity`` (written by
    ``ActivityTrackingMiddleware``) falls within ``ONLINE_WINDOW_SECONDS``.
    Sessions that are merely unexpired but idle are treated as offline.
    """
    from django.contrib.sessions.models import Session
    window = getattr(settings, 'ONLINE_WINDOW_SECONDS', 300)
    cutoff = timezone.now().timestamp() - window
    ids = set()
    now = timezone.now()
    for session in Session.objects.filter(expire_date__gt=now).only('session_data'):
        try:
            data = session.get_decoded()
        except Exception:
            continue
        uid = data.get('_auth_user_id')
        if not uid:
            continue
        last_activity = data.get('last_activity')
        if last_activity is None:
            continue
        try:
            if float(last_activity) < cutoff:
                continue
            ids.add(int(uid))
        except (TypeError, ValueError):
            continue
    return ids


@login_required
@_admin_required
def admin_users(request):
    return render(request, 'tenders/admin_users.html', {'active_tab': 'users'})


def _admin_user_dict(user, online_ids, request=None):
    is_online = user.pk in online_ids
    if not is_online and request is not None and user.pk == request.user.pk:
        is_online = True
    return {
        'id': user.pk,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': user.get_full_name() or user.email,
        'role': user.role,
        'role_label': user.get_role_display(),
        'phone': (user.profile.phone if hasattr(user, 'profile') else ''),
        'avatar_initials': user.avatar_initials(),
        'is_active': user.is_active,
        'is_staff': user.is_staff,
        'date_joined': user.date_joined.isoformat() if user.date_joined else None,
        'last_login': user.last_login.isoformat() if user.last_login else None,
        'is_online': is_online,
    }


@login_required
@_admin_required
def api_admin_users(request):
    online_ids = _online_user_ids()
    users = [
        _admin_user_dict(user, online_ids, request=request)
        for user in CustomUser.objects.select_related('profile').order_by('-last_login')
    ]
    return JsonResponse({
        'ok': True,
        'online_count': sum(1 for user in users if user['is_online']),
        'total_count': len(users),
        'users': users,
    })


@login_required
@_admin_required
@require_POST
def api_admin_user_password(request, user_id):
    """Reset a platform user's password on behalf of an administrator."""
    target = CustomUser.objects.filter(pk=user_id).first()
    if target is None:
        return JsonResponse({'ok': False, 'error': 'User not found.'}, status=404)
    raw = request.POST.get('password')
    if raw is None:
        try:
            raw = json.loads(request.body or b'{}').get('password')
        except (ValueError, TypeError):
            raw = None
    if not raw:
        return JsonResponse({'ok': False, 'error': 'A new password is required.'}, status=400)
    try:
        validate_password(raw, user=target)
    except ValidationError as exc:
        return JsonResponse({'ok': False, 'errors': list(exc.messages), 'error': exc.messages[0]}, status=400)
    target.set_password(raw)
    target.save(update_fields=['password'])
    return JsonResponse({
        'ok': True,
        'message': f'Password for {target.email} updated.',
        'user': _admin_user_dict(target, _online_user_ids(), request=request),
    })


# ---------------------------------------------------------------------------
# Diagnostics (administrator)
# ---------------------------------------------------------------------------

@login_required
@_admin_required
def admin_diagnostic(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        push_ids = None
        if action == 'retry_push':
            try:
                push_ids = [int(request.POST.get('push_id'))]
            except (TypeError, ValueError):
                messages.error(request, 'That submission could not be found.')
                return redirect('tenders:admin_diagnostic')
        delivered, failed, skipped = force_retry_pushes(push_ids=push_ids)
        messages.success(
            request,
            f'Re-sent {delivered} submission(s), {failed} failed again, {skipped} skipped '
            '(no base URL configured).',
        )
        return redirect('tenders:admin_diagnostic')
    pushes = PendingPush.objects.select_related('tender', 'tender__user').filter(
        state__in=(PendingPush.State.PENDING, PendingPush.State.FAILED),
    ).order_by('-created_at')
    return render(request, 'tenders/admin_diagnostics.html', {
        'active_tab': 'diagnostics',
        'pending_pushes': pushes,
        'pending_count': pushes.filter(state=PendingPush.State.PENDING).count(),
        'failed_count': pushes.filter(state=PendingPush.State.FAILED).count(),
    })


@login_required
@_admin_required
def api_admin_diagnostic(request):
    if request.method != 'GET':
        return JsonResponse({'ok': False, 'error': 'GET required.'}, status=405)

    base = ApiDiagnostic.objects.select_related('user')
    q = Q()

    api_point = (request.GET.get('api_point') or '').strip()
    if api_point:
        q &= Q(api_point=api_point)
    search = (request.GET.get('q') or '').strip()
    if search:
        q &= Q(user__email__icontains=search)

    api_points = list(base.values_list('api_point', flat=True).distinct().order_by('api_point'))
    emails = list(
        base.filter(user__isnull=False)
        .order_by()
        .values_list('user__email', flat=True)
        .distinct()
        .order_by('user__email')
    )
    if q:
        base = base.filter(q)

    per_page = 25
    count = base.count()
    page = max(_page_param(request), 1)
    pages = max((count + per_page - 1) // per_page, 1)
    page = min(page, pages)
    diagnostics = [
        {
            'id': d.pk,
            'user': _admin_user_dict(d.user, set(), request=request) if d.user else None,
            'api_point': d.api_point,
            'method': d.method,
            'path': d.path,
            'status_code': d.status_code,
            'message': d.message,
            'detail': d.detail,
            'created_at': d.created_at.isoformat(),
        }
        for d in base.order_by('-created_at')[(page - 1) * per_page:page * per_page]
    ]
    data = _page_meta(page, pages, per_page, count)
    today = timezone.localdate()
    today_count = ApiDiagnostic.objects.filter(created_at__date=today).count()
    data.update({
        'ok': True,
        'diagnostics': diagnostics,
        'api_points': api_points,
        'emails': emails,
        'today_count': today_count,
    })
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# Selcom payment gateway
# ---------------------------------------------------------------------------

def _invoice_in_scope(request, invoice):
    if invoice is None:
        return False
    if request.user.role == CustomUser.Role.ADMINISTRATOR:
        return True
    order = invoice.order
    if order.user_id and order.user_id == request.user.pk:
        return True
    if invoice.transporter_id and request.user.role == CustomUser.Role.AGENT:
        return invoice.transporter.agents.filter(pk=request.user.pk).exists()
    return False


def _transporter_matches_order(transporter, order):
    return bool(
        transporter.company_name
        and order.company_name
        and order.company_name.strip().lower() == transporter.company_name.strip().lower()
    )


def _agent_matches_order(user, order):
    return any(_transporter_matches_order(t, order) for t in user.linked_transporters.all())


def _payment_order_in_scope(request, order):
    if order is None:
        return False
    if request.user.role == CustomUser.Role.ADMINISTRATOR:
        return True
    if order.user_id and order.user_id == request.user.pk:
        return True
    if request.user.role == CustomUser.Role.AGENT:
        return _agent_matches_order(request.user, order)
    return order.user_id is None


def _set_invoice_paid(invoice, deposited_amount=None):
    if deposited_amount is None:
        deposited_amount = invoice.amount_total if invoice.amount_total is not None else Decimal('0.00')
    invoice.status = Invoice.Status.PAID
    invoice.deposited_amount = deposited_amount
    fields = ['status', 'selcom_status', 'deposited_amount']
    selcom_status = invoice.selcom_status or 'paid'
    invoice.selcom_status = selcom_status
    invoice.save(update_fields=fields)
    escrow = EscrowAccount.objects.filter(tender=invoice.order.tender).first()
    if escrow is not None:
        _refresh_escrow(escrow)
    _flush_derived_caches()


def _selcom_reference(invoice):
    reference = invoice.selcom_reference
    if not reference:
        reference = f'{invoice.number}-{invoice.pk}'
        invoice.selcom_reference = reference
        invoice.save(update_fields=('selcom_reference',))
    return reference


@login_required
@require_POST
def api_invoice_selcom_initiate(request, pk):
    invoice = Invoice.objects.select_related('order__tender', 'transporter', 'order__user').filter(pk=pk).first()
    if not _invoice_in_scope(request, invoice):
        return JsonResponse({'ok': False, 'error': 'Invoice not found.'}, status=404)
    return _initiate_selcom(request, invoice)


def _initiate_selcom(request, invoice):
    if invoice.status == Invoice.Status.PAID:
        return JsonResponse({'ok': False, 'error': 'This invoice is already paid.'})
    setting = _platform_setting()
    if not setting.selcom_enabled:
        return JsonResponse({'ok': False, 'error': 'Selcom payments are not enabled. Ask the administrator to configure them under Configuration > Selcom.'})

    if invoice.selcom_order_token and invoice.selcom_status not in ('created', 'pending', ''):
        if invoice.selcom_pay_link:
            return JsonResponse({
                'ok': True,
                'pay_link': invoice.selcom_pay_link,
                'order_token': invoice.selcom_order_token,
                'reference': invoice.selcom_reference,
            })

    reference = _selcom_reference(invoice)
    callback_url = request.build_absolute_uri(reverse('tenders:webhook_selcom'))
    redirect_url = request.build_absolute_uri(reverse('tenders:invoices'))
    try:
        result = selcom.create_checkout_order(setting, invoice, callback_url=callback_url, redirect_url=redirect_url)
    except selcom.SelcomError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)})

    invoice.selcom_order_token = result['order_token']
    invoice.selcom_pay_link = result['pay_link']
    invoice.selcom_status = 'created'
    invoice.selcom_updated_at = timezone.now()
    invoice.save(update_fields=('selcom_order_token', 'selcom_pay_link', 'selcom_status', 'selcom_updated_at'))
    escrow = EscrowAccount.objects.filter(tender=invoice.order.tender).first()
    if escrow is not None:
        _refresh_escrow(escrow)
    _flush_derived_caches()
    return JsonResponse({
        'ok': True,
        'message': 'Selcom checkout order created.',
        'pay_link': result['pay_link'],
        'order_token': result['order_token'],
        'reference': reference,
        'invoice': _invoice_dict(invoice),
    })


@login_required
@require_POST
def api_invoice_selcom_status(request, pk):
    invoice = Invoice.objects.select_related('order__tender', 'transporter', 'order__user').filter(pk=pk).first()
    if not _invoice_in_scope(request, invoice):
        return JsonResponse({'ok': False, 'error': 'Invoice not found.'}, status=404)
    if not invoice.selcom_order_token:
        return JsonResponse({'ok': False, 'error': 'No Selcom payment has been started for this invoice.'})
    setting = _platform_setting()
    if not setting.selcom_enabled:
        return JsonResponse({'ok': False, 'error': 'Selcom payments are not enabled.'})
    try:
        result = selcom.get_order_status(setting, invoice.selcom_order_token)
    except selcom.SelcomError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)})

    invoice.selcom_status = result['status'] or invoice.selcom_status
    invoice.selcom_updated_at = timezone.now()
    if result['paid']:
        _set_invoice_paid(invoice, selcom.collected_amount(result['parsed']))
    else:
        invoice.save(update_fields=('selcom_status', 'selcom_updated_at'))
    escrow = EscrowAccount.objects.filter(tender=invoice.order.tender).first()
    if escrow is not None:
        _refresh_escrow(escrow)
    _flush_derived_caches()
    return JsonResponse({
        'ok': True,
        'paid': invoice.status == Invoice.Status.PAID,
        'status': invoice.selcom_status,
        'invoice': _invoice_dict(invoice),
    })


@csrf_exempt
def webhook_selcom(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    raw_body = request.body
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid payload.'}, status=400)

    setting = _platform_setting()
    valid, reason = selcom.verify_callback(
        setting,
        raw_body,
        header_signature=request.META.get('HTTP_X_SELCOM_SIGNATURE', ''),
    )
    if not valid:
        return JsonResponse({'ok': False, 'error': 'Invalid callback signature.', 'reason': reason}, status=400)

    reference = (
        payload.get('vendor_reference_id')
        or (payload.get('reference') or '')
    )
    if not reference:
        return JsonResponse({'ok': False, 'error': 'Missing vendor_reference_id.'}, status=400)

    invoice = Invoice.objects.select_related('order').filter(selcom_reference=str(reference)).first()
    if invoice is None:
        return JsonResponse({'ok': False, 'error': 'Invoice not found for reference.'}, status=404)

    if not selcom.is_paid(payload):
        status = selcom.current_status(payload)
        invoice.selcom_status = status or invoice.selcom_status
        invoice.selcom_updated_at = timezone.now()
        invoice.save(update_fields=('selcom_status', 'selcom_updated_at'))
        return JsonResponse({'ok': True, 'paid': False, 'status': invoice.selcom_status})

    invoice.selcom_status = 'paid'
    invoice.selcom_updated_at = timezone.now()
    _set_invoice_paid(invoice, selcom.collected_amount(payload))
    return JsonResponse({'ok': True, 'paid': True, 'invoice': _invoice_dict(invoice)})
