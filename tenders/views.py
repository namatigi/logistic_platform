import concurrent.futures
import json
import logging
import math
from datetime import datetime
from decimal import Decimal

import requests as http
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, Sum, Count
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import (
    ApiSettingForm,
    IncomingEmailConfigForm,
    MediaConfigForm,
    OdooConfigForm,
    OutgoingEmailConfigForm,
    PaymentTermForm,
    SelcomConfigForm,
    TenderForm,
)
from .models import (
    ApiSetting,
    EscrowAccount,
    Invoice,
    Order,
    OrderLine,
    PaymentTerm,
    Tender,
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


def _shared_setting():
    return ApiSetting.get()


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
    if company_id:
        transporter = Transporter.objects.filter(company_id=company_id).first()
    if transporter is None and company_name:
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
        invoice = Invoice.objects.create(
            number=f'INV-{order.order_id}',
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
        if escrow.amount == 0 and invoice is not None and invoice.amount_total:
            escrow.amount = invoice.amount_total
            update_fields.append('amount')
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

    setting = _shared_setting()
    if setting is None or not setting.base_url:
        messages.warning(request, 'Configure your API base URL in Setting before awarding.')
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
            'transporter_alias': generate_transporter_alias(),
            'date_order': parse_datetime(payload.get('date_order')),
            'amount_total': to_decimal(payload.get('amount_total')),
            'customer': (tender.customer if tender else '') or payload.get('customer', '') or '',
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

    get_or_create_transporter(order)

    notify_order_update(order)

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
        context['api_setting'] = _shared_setting()
        return context

    def form_valid(self, form):
        tender = form.save(commit=False)
        tender.user = self.request.user

        setting = _shared_setting()
        if setting is None or not setting.base_url:
            messages.warning(
                self.request,
                'No API settings found. The administrator must configure the shared base URL and auth.',
            )
            tender.status = Tender.Status.PENDING
            tender.save()
            return redirect('tenders:create')
        if not setting.base_url:
            messages.warning(self.request, 'API base URL is missing.')
            tender.status = Tender.Status.PENDING
            tender.save()
            return redirect('tenders:create')

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


class ApiSettingUpdate(AdminRequiredMixin, LoginRequiredMixin, UpdateView):
    model = ApiSetting
    form_class = ApiSettingForm
    template_name = 'tenders/api_setting_form.html'
    success_url = reverse_lazy('tenders:api_settings')

    def get_object(self, queryset=None):
        return _shared_setting()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        path = reverse('tenders:webhook_orders')
        context['webhook_url'] = self.request.build_absolute_uri(path)
        context['webhook_path'] = path
        selcom_path = reverse('tenders:webhook_selcom')
        context['selcom_webhook_url'] = self.request.build_absolute_uri(selcom_path)
        return context

    def form_valid(self, form):
        messages.success(self.request, 'API settings saved.')
        return super().form_valid(form)


def _config_context(request, active):
    return {
        'active_config': active,
        'config_items': [
            (reverse('tenders:config_odoo'), 'Odoo', active == 'odoo'),
            (reverse('tenders:config_selcom'), 'Selcom', active == 'selcom'),
            (reverse('tenders:config_email'), 'Email', active == 'email'),
            (reverse('tenders:config_media'), 'Files', active == 'media'),
        ],
    }


@login_required
@_admin_required
def config_odoo(request):
    setting = _shared_setting()
    if request.method == 'POST':
        form = OdooConfigForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            _flush_derived_caches()
            messages.success(request, 'Odoo configuration saved.')
            return redirect('tenders:config_odoo')
    else:
        form = OdooConfigForm(instance=setting)
    context = _config_context(request, 'odoo')
    context['form'] = form
    context['webhook_url'] = request.build_absolute_uri(reverse('tenders:webhook_orders'))
    return render(request, 'tenders/config_odoo.html', context)


@login_required
@_admin_required
def config_selcom(request):
    setting = _shared_setting()
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
    setting = _shared_setting()
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
    setting = _shared_setting()
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
        'payment_terms': tender.payment_terms.name if tender.payment_terms_id else '',
        'payment_term_id': tender.payment_terms_id,
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
        'cargo_reference': order.cargo_reference,
        'cargo_id': order.cargo_id,
        'date_order': order.date_order.isoformat() if order.date_order else None,
        'created_at': order.created_at.isoformat() if order.created_at else None,
        'updated_at': order.updated_at.isoformat() if order.updated_at else None,
        'tender_ref': order.tender.cargo_reference if order.tender else None,
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
                'product_name': line.product_name,
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


def _setting_dict(setting, request):
    webhook_path = reverse('tenders:webhook_orders')
    selcom_webhook_path = reverse('tenders:webhook_selcom')
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
        'selcom_enabled': setting.selcom_enabled,
        'selcom_sandbox': setting.selcom_sandbox,
        'selcom_base_url': setting.selcom_base_url,
        'selcom_client_id': setting.selcom_client_id,
        'selcom_client_secret': setting.selcom_client_secret,
        'selcom_sales_channel': setting.selcom_sales_channel,
        'selcom_currency': setting.selcom_currency,
        'selcom_payment_methods': setting.selcom_payment_methods,
        'selcom_webhook_secret': setting.selcom_webhook_secret,
        'selcom_paylink_base': setting.selcom_paylink_base,
        'selcom_api_base': setting.selcom_api_base(),
        'selcom_payment_methods_list': setting.selcom_methods_list(),
        'selcom_webhook_path': selcom_webhook_path,
        'selcom_webhook_url': request.build_absolute_uri(selcom_webhook_path),
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
    if not hasattr(cache, 'delete_pattern'):
        return
    try:
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
    _flush_derived_caches()

    setting = _shared_setting()
    if setting is None or not setting.base_url:
        result = {
            'ok': True,
            'message': 'Tender saved locally. Configure your API base URL in Setting before sending.',
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
    page = _page_param(request)
    role = request.user.role
    scope = 'all' if role == CustomUser.Role.ADMINISTRATOR else request.user.pk

    def loader():
        per_page = 15
        base = Order.objects.filter(_visible_orders_q(request))
        total = base.count()
        pages = max((total + per_page - 1) // per_page, 1)
        p = min(page, pages)
        orders = (
            base.prefetch_related('tender', 'invoice')
            .annotate(
                _line_count=Count('lines'),
                _awarded_line_count=Count('lines', filter=Q(lines__awarded=True)),
                _awarded_amount_total=Sum('lines__price_total', filter=Q(lines__awarded=True)),
            )
            .order_by('-date_order', '-created_at')
            [(p - 1) * per_page: p * per_page]
        )
        grouped = {}
        for o in orders:
            key = o.tender.cargo_reference if o.tender else None
            grouped.setdefault(key, []).append(o)
        groups = [{
            'grouper': key or '',
            'label': key or 'Unlinked orders',
            'orders': [_order_dict(o) for o in items],
        } for key, items in grouped.items()]
        data = _page_meta(p, pages, per_page, total)
        data.update({'ok': True, 'groups': groups, 'total': len(orders)})
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

    setting = _shared_setting()
    if setting is None or not setting.base_url:
        return JsonResponse({
            'ok': False,
            'error': 'Configure your API base URL in Setting before awarding.',
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
    order = Order.objects.filter(pk=pk).first()
    if not _payment_order_in_scope(request, order):
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    invoice = get_or_create_invoice(order)
    return _confirm_invoice_paid(invoice)


@login_required
def api_order_checkout(request, pk):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    order = Order.objects.filter(pk=pk).first()
    if not _payment_order_in_scope(request, order):
        return JsonResponse({'ok': False, 'error': 'Order not found.'})
    invoice = get_or_create_invoice(order)
    return _initiate_selcom(request, invoice)


@login_required
def api_settings(request):
    setting = _shared_setting()
    if request.method == 'POST':
        if request.user.role != CustomUser.Role.ADMINISTRATOR:
            return JsonResponse({'ok': False, 'error': 'Administrator access required.'}, status=403)
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


# ---------------------------------------------------------------------------
# Route map
# ---------------------------------------------------------------------------

@login_required
def route_map(request):
    loading = request.GET.get('from', '')
    delivery = request.GET.get('to', '')
    tender_id = request.GET.get('tender_id')
    source = request.GET.get('source', '')
    truck = request.GET.get('truck', '')
    cargo_ref = request.GET.get('cargo_ref', '')

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
            truck_label = f'{tender.cargo_reference or f"T{tender.pk}"} \u00b7 T{truck_index}'
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
        pairs = {}
        for order in awarded_orders:
            tender = order.tender
            if tender is None:
                continue
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

        orders_out = []
        for order in awarded_orders:
            tender = order.tender
            entry = {
                'id': order.pk,
                'order_id': order.order_id,
                'order_name': order.order_name,
                'customer': order.customer,
                'cargo_reference': order.cargo_reference,
                'state': order.state or '',
                'tender_ref': tender.cargo_reference if tender else '',
                'awarded_amount': str(order._awarded_amount_total or 0),
                'awarded_lines_count': order._awarded_line_count,
                'total_lines_count': order._line_count,
                'fully_confirmed': order.fully_confirmed,
                'partially_confirmed': order.partially_confirmed,
            }
            trucks = []
            if tender is not None:
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
                    for i in range(truck_count):
                        stagger = i * 120
                        elapsed = max(0.0, (now - tender.created_at).total_seconds() - stagger)
                        progress = min(1.0, elapsed / sim_duration)
                        lat, lng = _position_at(route_points, route_distances, progress)
                        trucks.append({
                            'label': f'{tender.cargo_reference or f"T{tender.pk}"} · T{i + 1}',
                            'lat': lat,
                            'lng': lng,
                            'status': 'Delivered' if progress >= 1.0 else 'En route',
                            'progress': round(progress, 4),
                        })
                    entry.update({
                        'origin': {'name': origin.name, 'lat': origin.lat, 'lng': origin.lng},
                        'destination': {'name': dest.name, 'lat': dest.lat, 'lng': dest.lng},
                        'route': route_points,
                        'distance_km': round(route_km, 1),
                        'trucks': trucks,
                    })
            orders_out.append(entry)
        return {'ok': True, 'orders': orders_out, 'now': now.isoformat()}

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
        'cargo_reference': order.cargo_reference,
        'cargo_id': order.cargo_id,
        'route': f'{tender.route_loading} \u2192 {tender.route_delivery}' if tender else '',
        'tender_loading': tender.route_loading if tender else '',
        'tender_delivery': tender.route_delivery if tender else '',
        'tender_id': tender.pk if tender else None,
        'order_user': order.user.email if order.user else '',
        'selcom_reference': invoice.selcom_reference,
        'selcom_order_token': invoice.selcom_order_token,
        'selcom_pay_link': invoice.selcom_pay_link,
        'selcom_status': invoice.selcom_status,
        'selcom_updated_at': invoice.selcom_updated_at.isoformat() if invoice.selcom_updated_at else None,
        'lines': _invoice_lines(order),
    }


def _synthetic_invoice_dict(order):
    tender = order.tender
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
        'cargo_reference': order.cargo_reference,
        'cargo_id': order.cargo_id,
        'route': f'{tender.route_loading} \u2192 {tender.route_delivery}' if tender else '',
        'tender_loading': tender.route_loading if tender else '',
        'tender_delivery': tender.route_delivery if tender else '',
        'tender_id': tender.pk if tender else None,
        'order_user': order.user.email if order.user else '',
        'selcom_reference': None,
        'selcom_order_token': None,
        'selcom_pay_link': None,
        'selcom_status': None,
        'selcom_updated_at': None,
        'lines': _invoice_lines(order),
    }


def _payment_term_dict(term):
    return {
        'id': term.pk,
        'name': term.name,
        'description': term.description,
        'is_active': term.is_active,
        'items': [
            {'id': item.pk, 'text': item.text}
            for item in term.items.order_by('sort_order', 'created_at')
        ],
        'created_at': term.created_at.isoformat() if term.created_at else None,
    }


def _escrow_dict(escrow):
    invoices = list(escrow.invoices.select_related('order', 'transporter').order_by('created_at'))
    transporters = list(escrow.transporters.all())
    order = invoices[0].order if invoices else None
    tender = escrow.tender
    cargo_reference = ''
    if order and order.cargo_reference:
        cargo_reference = order.cargo_reference
    elif tender and tender.cargo_reference:
        cargo_reference = tender.cargo_reference
    transporter_names = [t.company_name or t.alias for t in transporters if t.company_name or t.alias]
    if not transporter_names and order and order.company_name:
        transporter_names = [order.company_name]
    tender_customer = tender.customer if tender else ''
    pending_tokens = [i.selcom_order_token for i in invoices if i.selcom_order_token]
    return {
        'id': escrow.pk,
        'virtual_account': escrow.virtual_account or '',
        'customer': tender_customer or '-',
        'transporter': ', '.join(transporter_names) or '-',
        'transporter_names': transporter_names,
        'amount': str(escrow.amount),
        'currency': invoices[0].currency if invoices else 'TZS',
        'payment_terms': escrow.payment_terms.name if escrow.payment_terms else '',
        'payment_terms_description': escrow.payment_terms.description if escrow.payment_terms else '',
        'created_at': escrow.created_at.isoformat() if escrow.created_at else None,
        'cargo_reference': cargo_reference or '',
        'invoice_numbers': [i.number for i in invoices],
        'invoice_number': ', '.join(i.number for i in invoices),
        'bank': escrow.bank or 'Selcom',
        'deposited_amount': str(escrow.deposited_amount),
        'status': escrow.status,
        'status_label': escrow.get_status_display(),
        'selcom_order_token': pending_tokens[0] if pending_tokens else '',
        'selcom_pay_link': invoices[0].selcom_pay_link if invoices else '',
        'selcom_status': invoices[0].selcom_status if invoices else '',
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
        'cargo_name': order.cargo_reference or '',
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
        .order_by('-awarded_at', '-created_at')
    )
    invoice_map = {
        inv.order_id: inv
        for inv in Invoice.objects.filter(order__in=awarded_orders).select_related('transporter')
    }
    rows = []
    for order in awarded_orders:
        invoice = invoice_map.get(order.pk)
        rows.append(_invoice_dict(invoice) if invoice else _synthetic_invoice_dict(order))
    return JsonResponse({
        'ok': True,
        'selcom_enabled': _shared_setting().selcom_enabled,
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
    return _confirm_invoice_paid(invoice)


def _confirm_invoice_paid(invoice):
    order = invoice.order
    setting = _shared_setting()
    if not setting.base_url:
        return JsonResponse({'ok': False, 'error': 'Configure the shared API base URL in Setting before confirming an invoice.'})

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
    update_fields = ['status']
    external_name = _external_invoice_number(parsed)
    if external_name:
        invoice.number = str(external_name)[:50]
        update_fields.append('number')
    invoice.save(update_fields=update_fields)
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


def _create_payment_term_items(term, raw_items):
    sort = 0
    if isinstance(raw_items, (list, tuple)):
        for raw in raw_items:
            text = ''
            if isinstance(raw, dict):
                text = str(raw.get('text') or '').strip()
            elif isinstance(raw, str):
                text = raw.strip()
            if not text:
                continue
            term.items.create(text=text[:300], sort_order=sort)
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
    last = term.items.order_by('-sort_order').first()
    sort = (last.sort_order + 1) if last else 0
    item = term.items.create(text=text[:300], sort_order=sort)
    return JsonResponse({
        'ok': True,
        'message': 'Term detail added.',
        'item': {'id': item.pk, 'text': item.text},
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
        .select_related('tender', 'user', 'payment_terms')
        .prefetch_related('invoices__order__tender', 'invoices__transporter', 'transporters')
        .order_by('-created_at')
    )
    return JsonResponse({'ok': True, 'escrow_accounts': [_escrow_dict(a) for a in accounts]})


# ---------------------------------------------------------------------------
# Users overview (administrator)
# ---------------------------------------------------------------------------

def _online_user_ids():
    from django.contrib.sessions.models import Session
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
        try:
            ids.add(int(uid))
        except (TypeError, ValueError):
            continue
    return ids


@login_required
@_admin_required
def admin_users(request):
    return render(request, 'tenders/admin_users.html', {'active_tab': 'users'})


@login_required
@_admin_required
def api_admin_users(request):
    online_ids = _online_user_ids()
    users = []
    for user in CustomUser.objects.select_related('profile').order_by('-last_login'):
        is_online = user.pk in online_ids
        if not is_online and user.pk == request.user.pk:
            is_online = True
        users.append({
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
        })
    return JsonResponse({
        'ok': True,
        'online_count': len(online_ids),
        'total_count': len(users),
        'users': users,
    })


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
    if transporter.company_id and order.company_id == transporter.company_id:
        return True
    if (
        transporter.company_name
        and order.company_name
        and order.company_name.strip().lower() == transporter.company_name.strip().lower()
    ):
        return True
    return False


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
    setting = _shared_setting()
    if not setting.selcom_enabled:
        return JsonResponse({'ok': False, 'error': 'Selcom payments are not enabled. Ask the administrator to configure them in the Setting page.'})

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
    setting = _shared_setting()
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

    setting = _shared_setting()
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
