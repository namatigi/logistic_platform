import json
import logging
from datetime import datetime
from decimal import Decimal
from itertools import groupby

import requests as http
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import ApiSettingForm, TenderForm
from .models import ApiSetting, Order, OrderLine, Tender
from .towns import TOWN_CHOICES

logger = logging.getLogger(__name__)


def build_payload(tender):
    return {
        'route_loading': tender.route_loading,
        'route_delivery': tender.route_delivery,
        'customer': tender.customer,
        'cargo_type': tender.cargo_type,
        'truck_type': tender.truck_type,
        'weight': tender.weight,
        'number_of_trucks': tender.number_of_trucks,
        'distance_km': tender.distance_km,
        'cargo_date': tender.cargo_date.isoformat(),
    }


def submit_tender(setting, tender):
    payload = build_payload(tender)
    headers = {'Content-Type': 'application/json'}
    auth = None

    if setting.auth_type == ApiSetting.AuthType.BEARER and setting.api_token:
        headers['Authorization'] = f'Bearer {setting.api_token}'
    elif setting.auth_type == ApiSetting.AuthType.BASIC:
        auth = (setting.username, setting.password)

    try:
        response = http.post(
            setting.endpoint_url(),
            json=payload,
            headers=headers,
            auth=auth,
            timeout=20,
        )
        return response.status_code, response.text, response.ok
    except http.RequestException as exc:
        logger.exception('Tender submission to %s failed', setting.endpoint_url())
        return None, str(exc), False


def submit_confirmation(setting, url, payload):
    headers = {'Content-Type': 'application/json'}
    auth = None

    if setting.auth_type == ApiSetting.AuthType.BEARER and setting.api_token:
        headers['Authorization'] = f'Bearer {setting.api_token}'
    elif setting.auth_type == ApiSetting.AuthType.BASIC:
        auth = (setting.username, setting.password)

    try:
        response = http.post(url, json=payload, headers=headers, auth=auth, timeout=20)
        return response.status_code, response.text, response.ok
    except http.RequestException as exc:
        logger.exception('Order confirmation to %s failed', url)
        return None, str(exc), False


def perform_award(order, setting, line_ids, is_partial):
    if is_partial and not line_ids:
        return {'ok': False, 'message': 'Select at least one order line to award.'}

    if line_ids:
        valid_ids = set(OrderLine.objects.filter(order=order).values_list('line_id', flat=True))
        if not set(line_ids).issubset(valid_ids):
            return {'ok': False, 'message': 'Some selected order lines do not belong to this order.'}

    cargo_name = (
        order.cargo_reference
        or (order.tender.cargo_reference if order.tender else '')
        or ''
    ).strip()
    if not cargo_name:
        return {'ok': False, 'message': 'This order has no cargo reference to confirm.'}

    payload = {
        'order_id': order.order_id,
        'message': 'Confirmed',
        'cargo_name': cargo_name,
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
            'message': f'Order confirmation failed (HTTP {status_code}). Response: {body[:300]}',
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

    setting = ApiSetting.objects.filter(user=request.user).first()
    if setting is None or not setting.base_url:
        messages.warning(request, 'Configure your API base URL in API Settings before awarding.')
        return redirect('tenders:api_settings')

    line_ids_raw = request.POST.getlist('line_ids')
    line_ids = [int(v) for v in line_ids_raw if str(v).strip().isdigit()]
    is_partial = request.POST.get('partial') == '1'
    result = perform_award(order, setting, line_ids, is_partial)
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
def webhook_orders(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST is allowed.'}, status=405)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)
    if not isinstance(payload, dict) or 'order_id' not in payload:
        return JsonResponse({'error': "Missing required field 'order_id'."}, status=400)

    cargo_reference = (payload.get('cargo_reference') or '').strip()
    tender = Tender.objects.filter(cargo_reference=cargo_reference).first() if cargo_reference else None

    order, created = Order.objects.update_or_create(
        order_id=payload['order_id'],
        defaults={
            'order_name': payload.get('order_name', '') or '',
            'state': payload.get('state', '') or '',
            'company_id': payload.get('company_id'),
            'company_name': payload.get('company_name', '') or '',
            'date_order': parse_datetime(payload.get('date_order')),
            'amount_total': to_decimal(payload.get('amount_total')),
            'customer': payload.get('customer', '') or '',
            'currency': payload.get('currency', '') or '',
            'cargo_reference': cargo_reference,
            'cargo_id': payload.get('cargo_id'),
            'user': tender.user if tender else None,
            'tender': tender,
            'raw_payload': payload,
        },
    )

    order.lines.all().delete()
    for line in payload.get('order_lines') or []:
        OrderLine.objects.create(
            order=order,
            line_id=line.get('line_id', 0),
            product_id=line.get('product_id'),
            product_name=line.get('product_name', '') or '',
            quantity=to_decimal(line.get('quantity')),
            price_unit=to_decimal(line.get('price_unit')),
            commission=to_decimal(line.get('commission')),
            price_subtotal=to_decimal(line.get('price_subtotal')),
            price_total=to_decimal(line.get('price_total')),
        )

    return JsonResponse({
        'status': 'ok',
        'created': created,
        'order_id': order.order_id,
        'cargo_reference': cargo_reference,
        'linked_tender': tender.cargo_reference if tender else None,
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


class TenderList(LoginRequiredMixin, ListView):
    model = Tender
    template_name = 'tenders/tender_list.html'
    context_object_name = 'tenders'
    paginate_by = 20

    def get_queryset(self):
        return Tender.objects.filter(user=self.request.user)


class TenderCreate(LoginRequiredMixin, CreateView):
    model = Tender
    form_class = TenderForm
    template_name = 'tenders/tender_form.html'
    success_url = reverse_lazy('tenders:create')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        setting = ApiSetting.objects.filter(user=self.request.user).first()
        context['api_setting'] = setting
        return context

    def form_valid(self, form):
        tender = form.save(commit=False)
        tender.user = self.request.user

        setting = ApiSetting.objects.filter(user=self.request.user).first()
        if setting is None:
            messages.warning(
                self.request,
                'No API settings found. Configure your base URL and auth before sending.',
            )
            tender.status = Tender.Status.PENDING
            tender.save()
            return redirect('tenders:api_settings')
        if not setting.base_url:
            messages.warning(self.request, 'API base URL is missing.')
            tender.status = Tender.Status.PENDING
            tender.save()
            return redirect('tenders:api_settings')

        status_code, body, _ok = submit_tender(setting, tender)
        tender.response_code = status_code
        tender.response_body = body[:4000]

        parsed = {}
        try:
            parsed = json.loads(body) if body else {}
        except (ValueError, TypeError):
            pass
        data = parsed.get('data') or {} if isinstance(parsed, dict) else {}
        tender.external_id = data.get('id') if isinstance(data, dict) else None
        tender.cargo_reference = data.get('name', '') if isinstance(data, dict) else ''
        tender.external_status = data.get('status', '') if isinstance(data, dict) else ''

        is_ok = status_code is not None and 200 <= status_code < 300
        if not is_ok and isinstance(parsed, dict) and parsed.get('status') == 'success':
            is_ok = True
        tender.status = Tender.Status.SUCCESS if is_ok else Tender.Status.FAILED
        tender.save()

        if is_ok:
            messages.success(
                self.request,
                f'Tender sent successfully to {setting.endpoint_url()} (HTTP {status_code}).'
                + (f' Reference: {tender.cargo_reference}.' if tender.cargo_reference else ''),
            )
        else:
            messages.error(
                self.request,
                f'Tender submission failed (HTTP {status_code}). Response: {body[:300]}',
            )
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
            .order_by('tender__cargo_reference', '-date_order', '-created_at')
        )


class OrderDetail(LoginRequiredMixin, DetailView):
    model = Order
    template_name = 'tenders/order_detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        return Order.objects.filter(
            Q(user=self.request.user) | Q(user__isnull=True)
        ).select_related('tender')


class ApiSettingUpdate(LoginRequiredMixin, UpdateView):
    model = ApiSetting
    form_class = ApiSettingForm
    template_name = 'tenders/api_setting_form.html'
    success_url = reverse_lazy('tenders:api_settings')

    def get_object(self, queryset=None):
        setting, _created = ApiSetting.objects.get_or_create(user=self.request.user)
        return setting

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        path = reverse('tenders:webhook_orders')
        context['webhook_url'] = self.request.build_absolute_uri(path)
        context['webhook_path'] = path
        return context

    def form_valid(self, form):
        messages.success(self.request, 'API settings saved.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# JSON API endpoints (used by the Vue front end)
# ---------------------------------------------------------------------------

def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return {}


def _tender_dict(tender):
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
        'cargo_reference': tender.cargo_reference,
        'response_code': tender.response_code,
        'created_at': tender.created_at.isoformat() if tender.created_at else None,
    }


def _order_dict(order, include_lines=False):
    data = {
        'id': order.pk,
        'order_id': order.order_id,
        'order_name': order.order_name,
        'state': order.state or '',
        'customer': order.customer,
        'company_name': order.company_name,
        'company_id': order.company_id,
        'currency': order.currency,
        'amount_total': str(order.amount_total),
        'awarded_amount': str(order.awarded_amount),
        'awarded_lines_count': order.awarded_lines_count,
        'total_lines_count': order.lines.count(),
        'cargo_reference': order.cargo_reference,
        'cargo_id': order.cargo_id,
        'date_order': order.date_order.isoformat() if order.date_order else None,
        'created_at': order.created_at.isoformat() if order.created_at else None,
        'updated_at': order.updated_at.isoformat() if order.updated_at else None,
        'tender_ref': order.tender.cargo_reference if order.tender else None,
        'tender_route': f"{order.tender.route_loading} -> {order.tender.route_delivery}" if order.tender else '',
        'fully_confirmed': order.fully_confirmed,
        'partially_confirmed': order.partially_confirmed,
        'remaining_line_ids': order.remaining_line_ids,
        'removed_line_ids': order.removed_line_ids,
        'awarded_at': order.awarded_at.isoformat() if order.awarded_at else None,
        'award_message': (order.award_response_data or {}).get('message', ''),
    }
    if include_lines:
        data['lines'] = [
            {
                'line_id': line.line_id,
                'product_id': line.product_id,
                'product_name': line.product_name,
                'quantity': str(line.quantity),
                'price_subtotal': str(line.price_subtotal),
                'price_total': str(line.price_total),
                'awarded': line.awarded,
            }
            for line in order.lines.order_by('line_id')
        ]
    return data


def _setting_dict(setting, request):
    webhook_path = reverse('tenders:webhook_orders')
    return {
        'id': setting.id,
        'base_url': setting.base_url,
        'auth_type': setting.auth_type,
        'api_token': setting.api_token,
        'username': setting.username,
        'password': setting.password,
        'updated_at': setting.updated_at.isoformat() if setting.updated_at else None,
        'endpoint': (setting.base_url.rstrip('/') if setting.base_url else '(base URL)') + '/api/v1/tenders',
        'webhook_path': webhook_path,
        'webhook_url': request.build_absolute_uri(webhook_path),
        'tenders_path': '/api/v1/tenders',
        'confirmation_path': '/api/v1/order-confirmation',
        'partial_confirmation_path': '/api/v1/partial-order-confirmation',
    }


def _form_meta():
    return {
        'towns': [{'value': value, 'label': label} for value, label in TOWN_CHOICES],
        'cargo_types': [{'value': c, 'label': Tender.CargoType(c).label} for c in Tender.CargoType.values],
        'truck_types': [{'value': c, 'label': Tender.TruckType(c).label} for c in Tender.TruckType.values],
        'auth_types': [{'value': c, 'label': ApiSetting.AuthType(c).label} for c in ApiSetting.AuthType.values],
    }


@login_required
def api_dashboard(request):
    recent = Tender.objects.filter(user=request.user)[:5]
    return JsonResponse({
        'ok': True,
        'email': request.user.email,
        'company_count': request.user.companies.count(),
        'tender_count': Tender.objects.filter(user=request.user).count(),
        'order_count': Order.objects.filter(
            Q(user=request.user) | Q(user__isnull=True)
        ).count(),
        'recent_tenders': [_tender_dict(t) for t in recent],
    })


@login_required
def api_tender_list(request):
    tenders = Tender.objects.filter(user=request.user)
    return JsonResponse({'ok': True, 'tenders': [_tender_dict(t) for t in tenders]})


@login_required
def api_form_meta(request):
    return JsonResponse({'ok': True, 'meta': _form_meta()})


@login_required
def api_tender_create(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    form = TenderForm(_json_body(request))
    if not form.is_valid():
        return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': form.errors})

    tender = form.save(commit=False)
    tender.user = request.user
    tender.save()

    setting = ApiSetting.objects.filter(user=request.user).first()
    if setting is None or not setting.base_url:
        result = {
            'ok': True,
            'message': 'Tender saved locally. Configure your API base URL in API Settings before sending.',
            'tender': _tender_dict(tender),
            'needs_settings': True,
        }
        return JsonResponse(result)

    status_code, body, _ok = submit_tender(setting, tender)
    tender.response_code = status_code
    tender.response_body = body[:4000]

    parsed = {}
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, TypeError):
        pass
    data = parsed.get('data') or {} if isinstance(parsed, dict) else {}
    tender.external_id = data.get('id') if isinstance(data, dict) else None
    tender.cargo_reference = data.get('name', '') if isinstance(data, dict) else ''
    tender.external_status = data.get('status', '') if isinstance(data, dict) else ''

    is_ok = status_code is not None and 200 <= status_code < 300
    if not is_ok and isinstance(parsed, dict) and parsed.get('status') == 'success':
        is_ok = True
    tender.status = Tender.Status.SUCCESS if is_ok else Tender.Status.FAILED
    tender.save()

    result = {'ok': True, 'tender': _tender_dict(tender)}
    if is_ok:
        result['message'] = (
            f'Tender sent successfully (HTTP {status_code}).'
            + (f' Reference: {tender.cargo_reference}.' if tender.cargo_reference else '')
        )
    else:
        result['message'] = f'Tender submission failed (HTTP {status_code}). Response: {body[:300]}'
    return JsonResponse(result)


@login_required
def api_order_list(request):
    orders = (
        Order.objects.filter(Q(user=request.user) | Q(user__isnull=True))
        .select_related('tender')
        .order_by('tender__cargo_reference', '-date_order', '-created_at')
    )
    groups = []
    for key, items in groupby(orders, key=lambda o: o.tender.cargo_reference if o.tender else None):
        items = list(items)
        groups.append({
            'grouper': key or '',
            'label': key or 'Unlinked orders',
            'orders': [_order_dict(o) for o in items],
        })
    return JsonResponse({'ok': True, 'groups': groups, 'total': len(orders)})


@login_required
def api_order_detail(request, pk):
    order = (
        Order.objects.filter(Q(user=request.user) | Q(user__isnull=True))
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
        Order.objects.filter(Q(user=request.user) | Q(user__isnull=True))
        .filter(pk=pk)
        .first()
    )
    if order is None:
        return JsonResponse({'ok': False, 'error': 'Order not found.'})

    setting = ApiSetting.objects.filter(user=request.user).first()
    if setting is None or not setting.base_url:
        return JsonResponse({
            'ok': False,
            'error': 'Configure your API base URL in API Settings before awarding.',
            'redirect': reverse('tenders:api_settings'),
        })

    data = _json_body(request)
    try:
        line_ids = [int(x) for x in (data.get('line_ids') or []) if str(x).isdigit()]
    except (TypeError, ValueError):
        line_ids = []
    is_partial = bool(data.get('partial')) or bool(line_ids)

    result = perform_award(order, setting, line_ids, is_partial)
    if not result['ok']:
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
    order = (
        Order.objects.filter(Q(user=request.user) | Q(user__isnull=True))
        .filter(pk=pk)
        .first()
    )
    if order is None:
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    return JsonResponse({
        'ok': True,
        'message': f'Payment for order {order.order_name or order.order_id} is not available yet.',
    })


@login_required
def api_settings(request):
    setting, _created = ApiSetting.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = ApiSettingForm(_json_body(request), instance=setting)
        if not form.is_valid():
            return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': form.errors})
        form.save()
        return JsonResponse({
            'ok': True,
            'message': 'API settings saved.',
            'setting': _setting_dict(setting, request),
        })
    return JsonResponse({'ok': True, 'setting': _setting_dict(setting, request)})