import json
import logging
from datetime import datetime
from decimal import Decimal

import requests as http
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import ApiSettingForm, TenderForm
from .models import ApiSetting, Order, OrderLine, Tender

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
        return Order.objects.filter(
            Q(user=self.request.user) | Q(user__isnull=True)
        ).select_related('tender')


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