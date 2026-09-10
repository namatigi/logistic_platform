import json
import secrets
from io import BytesIO
from urllib.parse import urlencode

import requests
from PIL import Image, ImageOps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from .forms import AddressForm, ProfileForm, SignUpForm
from .models import Address, CustomUser, Profile
from tenders.models import ApiSetting, Invoice, Order, OrderLine, Tender, Town, Transporter, Truck, TruckModel
from tenders.views import (
    SIM_ACCELERATION,
    SIM_SPEED_KMH,
    _invoice_dict,
    _position_at,
    _route_arrays,
    _shared_setting,
    get_or_create_invoice,
    get_route,
)

from django.urls import reverse

MAX_PROFILE_SIZE = (512, 512)


def _default_landing_url(user):
    if getattr(user, 'role', None) == CustomUser.Role.AGENT:
        return reverse('users:agent_awarded')
    return reverse('tenders:dashboard')


def _resize_profile_picture(uploaded):
    image = Image.open(uploaded)
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail(MAX_PROFILE_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format='JPEG', quality=82, optimize=True)
    buffer.seek(0)
    base_name = (uploaded.name or 'profile').rsplit('.', 1)[0].replace('\\', '/').split('/')[-1]
    return ContentFile(buffer.read(), name=f'{base_name}.jpg')


class EmailLoginView(LoginView):
    template_name = 'registration/login.html'
    redirect_authenticated_user = True

    def get_success_url(self):
        return _default_landing_url(self.request.user)


email_login = ensure_csrf_cookie(EmailLoginView.as_view())


GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'


def google_login(request):
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id:
        messages.error(request, 'Sign in with Google is not configured yet. Please use your email instead.')
        return redirect('users:login')
    state = get_random_string(32)
    request.session['google_oauth_state'] = state
    redirect_uri = request.build_absolute_uri(reverse('users:google_callback'))
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'access_type': 'online',
        'state': state,
        'prompt': 'select_account',
    }
    return redirect(GOOGLE_AUTH_URL + '?' + urlencode(params))


def google_callback(request):
    state = request.GET.get('state')
    session_state = request.session.pop('google_oauth_state', None)
    if not state or state != session_state:
        messages.error(request, 'Google sign-in failed: the request could not be verified. Please try again.')
        return redirect('users:login')
    if request.GET.get('error') or not request.GET.get('code'):
        messages.error(request, 'Google sign-in was cancelled or failed. Please try again.')
        return redirect('users:login')
    redirect_uri = request.build_absolute_uri(reverse('users:google_callback'))
    data = {
        'code': request.GET['code'],
        'client_id': getattr(settings, 'GOOGLE_CLIENT_ID', ''),
        'client_secret': getattr(settings, 'GOOGLE_CLIENT_SECRET', ''),
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
    }
    try:
        token = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=15).json()
    except requests.RequestException:
        messages.error(request, 'Google sign-in failed: could not reach Google. Please try again.')
        return redirect('users:login')
    id_token = token.get('id_token')
    if not id_token:
        messages.error(request, 'Google sign-in failed. Please try again.')
        return redirect('users:login')
    try:
        info = requests.get(GOOGLE_TOKENINFO_URL, params={'id_token': id_token}, timeout=15).json()
    except requests.RequestException:
        messages.error(request, 'Google sign-in failed: could not verify your identity. Please try again.')
        return redirect('users:login')
    email = (info.get('email') or '').strip().lower()
    if not email:
        messages.error(request, 'Google sign-in failed: your Google account has no email address.')
        return redirect('users:login')
    user = CustomUser.objects.filter(email__iexact=email).first()
    if user is None:
        user = CustomUser.objects.create_user(
            email=email,
            password=None,
            first_name=(info.get('given_name') or '').strip()[:150],
            last_name=(info.get('family_name') or '').strip()[:150],
        )
    if not user.is_active:
        messages.error(request, 'This account is disabled.')
        return redirect('users:login')
    user.backend = 'django.contrib.auth.backends.ModelBackend'
    login(request, user)
    return redirect(_default_landing_url(user))


@ensure_csrf_cookie
def signup(request):
    if request.user.is_authenticated:
        return redirect(_default_landing_url(request.user))
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect(_default_landing_url(user))
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        post = getattr(request, 'POST', None)
        if post:
            return {key: post.getlist(key) if len(post.getlist(key)) > 1 else post.get(key) for key in post.keys()}
        return {}


@login_required
def admin_dashboard(request):
    if request.user.role != CustomUser.Role.ADMINISTRATOR:
        return redirect('users:profile')
    return render(request, 'users/admin_dashboard.html', {'active_tab': 'admin'})


def _transporter_dict(transporter):
    awarded_orders = Order.objects.filter(
        lines__awarded=True,
    ).filter(
        Q(company_id=transporter.company_id) | Q(company_name__iexact=transporter.company_name)
    ).distinct().count()
    return {
        'id': transporter.pk,
        'company_name': transporter.company_name,
        'alias': transporter.alias,
        'company_id': transporter.company_id,
        'awarded_orders': awarded_orders,
        'agent_count': transporter.agents.count(),
    }


def _agent_dict(user):
    profile = getattr(user, 'profile', None)
    return {
        'id': user.pk,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'phone': profile.phone if profile else '',
        'bio': profile.bio if profile else '',
        'picture': profile.profile_picture.url if (profile and profile.profile_picture) else '',
        'created_at': user.date_joined.isoformat(),
        'linked': sorted(user.linked_transporters.values_list('id', flat=True)),
    }


@login_required
def api_admin_dashboard(request):
    if request.user.role != CustomUser.Role.ADMINISTRATOR:
        return JsonResponse({'ok': False, 'error': 'Administrator access required.'}, status=403)
    transporters = Transporter.objects.order_by('company_name', 'alias')
    agents = CustomUser.objects.filter(role=CustomUser.Role.AGENT).select_related('profile').order_by('-date_joined')
    return JsonResponse({
        'ok': True,
        'transporters': [_transporter_dict(t) for t in transporters],
        'agents': [_agent_dict(a) for a in agents],
    })


@login_required
@require_POST
def api_admin_agent_create(request):
    if request.user.role != CustomUser.Role.ADMINISTRATOR:
        return JsonResponse({'ok': False, 'error': 'Administrator access required.'}, status=403)
    email = CustomUser.objects.normalize_email((request.POST.get('email') or '').strip())
    first_name = (request.POST.get('first_name') or '').strip()[:150]
    last_name = (request.POST.get('last_name') or '').strip()[:150]
    phone = (request.POST.get('phone') or '').strip()
    bio = (request.POST.get('bio') or '').strip()
    password = request.POST.get('password') or ''
    if not email or '@' not in email or not first_name:
        return JsonResponse({'ok': False, 'error': 'Email and first name are required.'})
    if password and len(password) < 8:
        return JsonResponse({'ok': False, 'error': 'Password must be at least 8 characters long.'})
    if CustomUser.objects.filter(email=email).exists():
        return JsonResponse({'ok': False, 'error': 'A user with that email already exists.'})
    if not password:
        password = secrets.token_urlsafe(8)
    user = CustomUser.objects.create_user(
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        role=CustomUser.Role.AGENT,
    )
    profile = Profile.objects.create(user=user, phone=phone, bio=bio)
    uploaded = request.FILES.get('profile_picture')
    if uploaded:
        try:
            resized = _resize_profile_picture(uploaded)
            profile.profile_picture.save(resized.name, resized, save=False)
            profile.save()
        except Exception:
            user.delete()
            return JsonResponse({'ok': False, 'error': 'The uploaded picture could not be processed.'})
    _send_agent_credentials(email, password)
    return JsonResponse({
        'ok': True,
        'message': f'Agent {first_name} registered successfully.',
        'agent': _agent_dict(user),
        'password': password,
    })


def _send_agent_credentials(email, password):
    subject = 'Your HYPAX agent account'
    message = (
        'Welcome to HYPAX.\n\n'
        'An agent account was created for you.\n\n'
        f'Email: {email}\n'
        f'Password: {password}\n\n'
        'You can sign in at the HYPAX login page with these credentials.'
    )
    try:
        from_email = ApiSetting.get().email_from or settings.DEFAULT_FROM_EMAIL
        send_mail(subject, message, from_email, [email], fail_silently=False)
    except Exception:
        pass


@login_required
@require_POST
def api_admin_agent_transporters(request, pk):
    if request.user.role != CustomUser.Role.ADMINISTRATOR:
        return JsonResponse({'ok': False, 'error': 'Administrator access required.'}, status=403)
    agent = CustomUser.objects.filter(pk=pk, role=CustomUser.Role.AGENT).first()
    if agent is None:
        return JsonResponse({'ok': False, 'error': 'Agent not found.'})
    body = _json_body(request)
    transporter_ids = body.get('transporter_ids') or body.get('company_ids') or []
    try:
        transporter_ids = [int(i) for i in transporter_ids]
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Invalid transporter selection.'})
    transporters = Transporter.objects.filter(pk__in=transporter_ids)
    agent.linked_transporters.set(transporters)
    return JsonResponse({
        'ok': True,
        'message': f'{agent.first_name} {agent.last_name} linked to {transporters.count()} transporter(s).',
        'linked': sorted(agent.linked_transporters.values_list('id', flat=True)),
    })


def _agent_match_q(agent):
    q = Q()
    for transporter in agent.linked_transporters.all():
        per = Q()
        if transporter.company_id:
            per |= Q(company_id=transporter.company_id)
        if transporter.company_name:
            per |= Q(company_name__iexact=transporter.company_name)
        q |= per
    return q


def agent_awarded(request):
    return render(request, 'users/agent_awarded.html', {'active_tab': 'agents'})


def agent_tracker(request):
    return render(request, 'users/agent_tracker.html', {'active_tab': 'agents'})


def agent_invoices(request):
    return render(request, 'users/agent_invoices.html', {'active_tab': 'agents'})


@login_required
def api_agent_awarded(request):
    q = _agent_match_q(request.user)
    if not q:
        return JsonResponse({'ok': True, 'orders': []})
    orders = (
        Order.objects.filter(q, lines__awarded=True)
        .select_related('tender')
        .distinct()
        .order_by('-created_at')
    )
    out = []
    for order in orders:
        tender = order.tender
        lines_total = order.lines.count()
        awarded_lines = order.lines.filter(awarded=True).count()
        confirmation = 'partial'
        if awarded_lines and awarded_lines >= lines_total:
            confirmation = 'full'
        out.append({
            'id': order.pk,
            'order_id': order.order_id,
            'order_name': order.order_name or f'#{order.order_id}',
            'cargo_reference': order.cargo_reference or '',
            'tender_ref': tender.cargo_reference if tender else '',
            'customer': tender.customer if tender else order.customer,
            'route': f'{tender.route_loading} \u2192 {tender.route_delivery}' if tender else '',
            'cargo_type': tender.get_cargo_type_display() if tender else '',
            'truck_type': tender.get_truck_type_display() if tender else '',
            'weight': tender.weight if tender else None,
            'number_of_trucks': tender.number_of_trucks if tender else None,
            'company_name': order.company_name,
            'state': order.state or '',
            'awarded_amount': str(order.awarded_amount),
            'lines_total': lines_total,
            'awarded_lines': awarded_lines,
            'confirmation': confirmation,
            'confirmation_label': 'Fully confirmed' if confirmation == 'full' else 'Partially confirmed',
            'created_at': order.created_at.isoformat(),
        })
    return JsonResponse({'ok': True, 'orders': out})


@login_required
def api_agent_tracker(request):
    now = timezone.now()
    q = _agent_match_q(request.user)
    if not q:
        return JsonResponse({'ok': True, 'orders': []})
    towns_by_name = {t.name: t for t in Town.objects.all()}
    orders = (
        Order.objects.filter(q, lines__awarded=True)
        .select_related('tender')
        .distinct()
        .order_by('-created_at')
    )
    out = []
    for order in orders:
        tender = order.tender
        entry = {
            'id': order.pk,
            'order_id': order.order_id,
            'order_name': order.order_name,
            'customer': order.customer,
            'company_name': order.company_name,
            'cargo_reference': order.cargo_reference,
            'tender_ref': tender.cargo_reference if tender else '',
            'state': order.state or '',
            'awarded_amount': str(order.awarded_amount),
        }
        trucks = []
        if tender is not None:
            origin = towns_by_name.get(tender.route_loading)
            dest = towns_by_name.get(tender.route_delivery)
            if origin is not None and dest is not None:
                route_points, route_m = get_route(origin, dest)
                route_km = route_m / 1000.0
                if not route_km:
                    route_km = float(tender.distance_km or 0)
                sim_duration = max((route_km / SIM_SPEED_KMH) * 3600 / SIM_ACCELERATION, 3.0)
                route_distances = _route_arrays(route_points)
                for i in range(max(tender.number_of_trucks or 1, 1)):
                    stagger = i * 120
                    elapsed = max(0.0, (now - tender.created_at).total_seconds() - stagger)
                    progress = min(1.0, elapsed / sim_duration)
                    lat, lng = _position_at(route_points, route_distances, progress)
                    trucks.append({
                        'label': f'{tender.cargo_reference or f"T{tender.pk}"} \u00b7 T{i + 1}',
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
        out.append(entry)
    return JsonResponse({'ok': True, 'orders': out, 'now': now.isoformat()})


@login_required
def api_agent_invoices(request):
    transporters = list(request.user.linked_transporters.all())
    if not transporters:
        return JsonResponse({'ok': True, 'invoices': []})
    q = Q()
    for transporter in transporters:
        per = Q()
        if transporter.company_id:
            per |= Q(company_id=transporter.company_id)
        if transporter.company_name:
            per |= Q(company_name__iexact=transporter.company_name)
        q |= per
    awarded_orders = Order.objects.filter(q, lines__awarded=True).select_related('tender').distinct()
    for order in awarded_orders:
        get_or_create_invoice(order)
    invoices = (
        Invoice.objects.filter(transporter__in=transporters)
        .select_related('order__tender', 'transporter')
        .order_by('-created_at')
    )
    setting = _shared_setting()
    return JsonResponse({'ok': True, 'selcom_enabled': setting.selcom_enabled, 'invoices': [_invoice_dict(i) for i in invoices]})


@login_required
@require_POST
def api_agent_invoice_paid(request, pk):
    invoice = Invoice.objects.filter(pk=pk, transporter__agents=request.user).first()
    if invoice is None:
        return JsonResponse({'ok': False, 'error': 'Invoice not found.'}, status=404)
    invoice.status = Invoice.Status.PAID
    invoice.save(update_fields=('status',))
    return JsonResponse({'ok': True, 'message': 'Invoice marked as paid.', 'invoice': _invoice_dict(invoice)})


@login_required
def agent_transporters(request):
    if request.user.role != CustomUser.Role.AGENT:
        return redirect('users:profile')
    return render(request, 'users/agent_transporters.html', {'active_tab': 'agent_transporters'})


def _whole_value(data, name, errors):
    value = data.get(name)
    if value in (None, ''):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors[name] = [f'{name.replace("_", " ").title()} must be a whole number.']
        return None
    if number < 0:
        errors[name] = [f'{name.replace("_", " ").title()} must be a positive number.']
        return None
    return number


def _decimal_value(data, name, errors):
    value = data.get(name)
    if value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors[name] = [f'{name.replace("_", " ").title()} must be a number.']
        return None
    if number < 0:
        errors[name] = [f'{name.replace("_", " ").title()} must be a positive number.']
        return None
    return number


def _truck_model_dict(model):
    return {
        'id': model.pk,
        'name': model.name,
        'manufacturer': model.manufacturer,
        'vehicle_type': model.vehicle_type,
        'vehicle_type_label': model.get_vehicle_type_display(),
        'model_year': model.model_year,
        'volume_capacity': model.volume_capacity,
        'tonnage_capacity': model.tonnage_capacity,
        'number_of_axles': model.number_of_axles,
        'fuel_type': model.fuel_type,
        'fuel_type_label': model.get_fuel_type_display(),
        'transmission': model.transmission,
        'transmission_label': model.get_transmission_display(),
        'drive_type': model.drive_type,
        'drive_type_label': model.get_drive_type_display(),
    }


def _truck_dict(truck):
    return {
        'id': truck.pk,
        'model_id': truck.truck_model_id,
        'model': truck.model,
        'license_plate': truck.license_plate,
        'tags': truck.tags,
        'chassis_number': truck.chassis_number,
        'model_year': truck.model_year,
        'tonnage_capacity': truck.tonnage_capacity,
        'number_of_axles': truck.number_of_axles,
        'volume_capacity': truck.volume_capacity,
        'truck_type': truck.truck_type,
        'truck_type_label': truck.get_truck_type_display(),
        'created_at': truck.created_at.isoformat() if truck.created_at else None,
    }


def _truck_matches_type(truck, truck_type):
    if not truck_type:
        return True
    return not truck.truck_type or truck.truck_type == truck_type


def _agent_truck_assignments(user, now):
    transporters = list(user.linked_transporters.all())
    if not transporters:
        return {}
    towns_by_name = {t.name: t for t in Town.objects.all()}
    resolved_routes = {}
    assignments = {}
    for transporter in transporters:
        trucks = sorted(transporter.trucks.all(), key=lambda t: (t.created_at, t.pk))
        if not trucks:
            continue
        per = Q()
        if transporter.company_id:
            per |= Q(company_id=transporter.company_id)
        if transporter.company_name:
            per |= Q(company_name__iexact=transporter.company_name)
        if not per:
            continue
        orders = (
            Order.objects.filter(per, lines__awarded=True)
            .select_related('tender')
            .distinct()
            .order_by('created_at', 'pk')
        )
        busy = set()
        for order in orders:
            tender = order.tender
            if tender is None:
                continue
            origin = towns_by_name.get(tender.route_loading)
            dest = towns_by_name.get(tender.route_delivery)
            if origin is None or dest is None:
                continue
            route_key = (origin.name, dest.name)
            if route_key not in resolved_routes:
                resolved_routes[route_key] = get_route(origin, dest)
            route_points, route_m = resolved_routes[route_key]
            route_km = route_m / 1000.0
            if not route_km:
                route_km = float(tender.distance_km or 0)
            sim_duration = max((route_km / SIM_SPEED_KMH) * 3600 / SIM_ACCELERATION, 3.0)
            route_distances = _route_arrays(route_points)
            eligible = [t for t in trucks if _truck_matches_type(t, tender.truck_type)]
            chosen = [t for t in eligible if t.pk not in busy][:max(tender.number_of_trucks or 1, 1)]
            for slot, truck in enumerate(chosen):
                busy.add(truck.pk)
                stagger = slot * 120
                elapsed = max(0.0, (now - tender.created_at).total_seconds() - stagger)
                progress = min(1.0, elapsed / sim_duration)
                lat, lng = _position_at(route_points, route_distances, progress)
                assignments[truck.pk] = {
                    'active': True,
                    'order_id': order.order_id,
                    'order_name': order.order_name or '',
                    'cargo_reference': order.cargo_reference or '',
                    'tender_ref': tender.cargo_reference or '',
                    'state': order.state or '',
                    'origin': {'name': origin.name, 'lat': origin.lat, 'lng': origin.lng},
                    'destination': {'name': dest.name, 'lat': dest.lat, 'lng': dest.lng},
                    'route': route_points,
                    'distance_km': round(route_km, 1),
                    'progress': round(progress, 4),
                    'status': 'Delivered' if progress >= 1.0 else 'En route',
                    'lat': lat,
                    'lng': lng,
                    'slot': slot + 1,
                }
    return assignments


@login_required
def api_agent_transporters(request):
    transporters = request.user.linked_transporters.all()
    return JsonResponse({
        'ok': True,
        'transporters': [
            {
                'id': t.pk,
                'company_name': t.company_name,
                'alias': t.alias,
                'company_id': t.company_id,
                'trucks': [_truck_dict(truck) for truck in t.trucks.all()],
            }
            for t in transporters
        ],
    })


@login_required
@require_POST
def api_agent_transporter_trucks(request, pk):
    transporter = request.user.linked_transporters.filter(pk=pk).first()
    if transporter is None:
        return JsonResponse({'ok': False, 'error': 'Transporter not found.'}, status=404)
    data = _json_body(request)
    errors = {}

    truck_model = None
    model_id = data.get('model_id')
    if model_id not in (None, ''):
        truck_model = TruckModel.objects.filter(pk=model_id).first()
        if truck_model is None:
            errors['model_id'] = ['Select a valid model.']

    model_text = (data.get('model') or '').strip()[:120]
    if not model_text and not truck_model:
        errors['model'] = ['Model is required.']

    required = ('license_plate', 'tags', 'chassis_number', 'model_year',
                'tonnage_capacity', 'number_of_axles', 'truck_type')
    for field in required:
        raw = data.get(field)
        empty = raw in (None, '') or (isinstance(raw, str) and not raw.strip())
        if empty:
            errors[field] = [f'{field.replace("_", " ").title()} is required.']

    model_year = _whole_value(data, 'model_year', errors)
    number_of_axles = _whole_value(data, 'number_of_axles', errors)
    tonnage_capacity = _decimal_value(data, 'tonnage_capacity', errors)
    volume_capacity = _decimal_value(data, 'volume_capacity', errors)

    truck_type = (data.get('truck_type') or '').strip()
    if truck_type and truck_type not in Tender.TruckType.values:
        errors['truck_type'] = ['Choose a valid truck type.']

    if errors:
        return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': errors})

    model_name = model_text or (truck_model.name if truck_model else '')
    truck = Truck.objects.create(
        transporter=transporter,
        truck_model=truck_model,
        model=model_name,
        license_plate=(data.get('license_plate') or '').strip()[:40],
        tags=(data.get('tags') or '').strip()[:200],
        chassis_number=(data.get('chassis_number') or '').strip()[:120],
        model_year=model_year,
        tonnage_capacity=tonnage_capacity,
        number_of_axles=number_of_axles,
        volume_capacity=volume_capacity,
        truck_type=truck_type,
    )
    return JsonResponse({'ok': True, 'message': 'Truck added successfully.', 'truck': _truck_dict(truck)})


@login_required
def api_agent_truck_models(request):
    query = (request.GET.get('q') or '').strip()
    models_qs = TruckModel.objects.all()
    if query:
        models_qs = models_qs.filter(Q(name__icontains=query) | Q(manufacturer__icontains=query))
    return JsonResponse({'ok': True, 'models': [_truck_model_dict(m) for m in models_qs[:50]]})


@login_required
@require_POST
def api_agent_truck_models_create(request):
    data = _json_body(request)
    errors = {}

    name = (data.get('name') or '').strip()[:150]
    if not name:
        errors['name'] = ['Model name is required.']
    manufacturer = (data.get('manufacturer') or '').strip()[:150]
    vehicle_type = (data.get('vehicle_type') or '').strip()
    if vehicle_type and vehicle_type not in Tender.TruckType.values:
        errors['vehicle_type'] = ['Choose a valid vehicle type.']
    fuel_type = (data.get('fuel_type') or '').strip()
    if fuel_type and fuel_type not in TruckModel.FuelType.values:
        errors['fuel_type'] = ['Choose a valid fuel type.']
    transmission = (data.get('transmission') or '').strip()
    if transmission and transmission not in TruckModel.Transmission.values:
        errors['transmission'] = ['Choose a valid transmission.']
    drive_type = (data.get('drive_type') or '').strip()
    if drive_type and drive_type not in TruckModel.DriveType.values:
        errors['drive_type'] = ['Choose a valid drive type.']

    model_year = _whole_value(data, 'model_year', errors)
    number_of_axles = _whole_value(data, 'number_of_axles', errors)
    volume_capacity = _decimal_value(data, 'volume_capacity', errors)
    tonnage_capacity = _decimal_value(data, 'tonnage_capacity', errors)

    if errors:
        return JsonResponse({'ok': False, 'error': 'Please fix the highlighted fields.', 'errors': errors})

    model = TruckModel.objects.create(
        name=name,
        manufacturer=manufacturer,
        vehicle_type=vehicle_type,
        model_year=model_year,
        volume_capacity=volume_capacity,
        tonnage_capacity=tonnage_capacity,
        number_of_axles=number_of_axles,
        fuel_type=fuel_type,
        transmission=transmission,
        drive_type=drive_type,
    )
    return JsonResponse({'ok': True, 'message': 'Model created successfully.', 'model': _truck_model_dict(model)})


def _truck_transporter_label(transporter):
    return transporter.company_name or transporter.alias or f'#{transporter.company_id or transporter.pk}'


@login_required
def agent_trucks(request):
    if request.user.role != CustomUser.Role.AGENT:
        return redirect('users:profile')
    return render(request, 'users/agent_trucks.html', {'active_tab': 'agent_trucks'})


@login_required
def agent_truck_track(request, pk):
    if request.user.role != CustomUser.Role.AGENT:
        return redirect('users:profile')
    if not Truck.objects.filter(pk=pk, transporter__agents=request.user).exists():
        raise Http404()
    return render(request, 'users/agent_truck_track.html', {'active_tab': 'agent_trucks'})


@login_required
def api_agent_trucks(request):
    now = timezone.now()
    assignments = _agent_truck_assignments(request.user, now)
    trucks = (
        Truck.objects.filter(transporter__agents=request.user)
        .select_related('transporter', 'truck_model')
        .order_by('-created_at')
    )
    out = []
    for truck in trucks:
        row = _truck_dict(truck)
        transporter = truck.transporter
        row['transporter_id'] = transporter.pk
        row['transporter'] = _truck_transporter_label(transporter)
        a = assignments.get(truck.pk)
        row['tracking'] = {
            'active': a is not None,
            'status': a['status'] if a else 'Idle',
            'progress': a['progress'] if a else None,
            'order_id': a['order_id'] if a else '',
            'order_name': a['order_name'] if a else '',
            'cargo_reference': a['cargo_reference'] if a else '',
            'route_text': f"{a['origin']['name']} \u2192 {a['destination']['name']}" if a else '',
        }
        out.append(row)
    return JsonResponse({'ok': True, 'trucks': out, 'now': now.isoformat()})


@login_required
@require_POST
def api_agent_truck_delete(request, pk):
    truck = (
        Truck.objects.filter(pk=pk, transporter__agents=request.user)
        .select_related('transporter')
        .first()
    )
    if truck is None:
        return JsonResponse({'ok': False, 'error': 'Truck not found.'}, status=404)
    plate = truck.license_plate or truck.model or f'Truck #{truck.pk}'
    plate = f'{plate} ({truck.transporter.company_name or truck.transporter.alias or "unknown transporter"})'
    truck.delete()
    return JsonResponse({'ok': True, 'message': f'Truck {plate} deleted.', 'id': pk})


@login_required
def api_agent_truck_track(request, pk):
    truck = (
        Truck.objects.filter(pk=pk, transporter__agents=request.user)
        .select_related('transporter', 'truck_model')
        .first()
    )
    if truck is None:
        return JsonResponse({'ok': False, 'error': 'Truck not found.'}, status=404)
    now = timezone.now()
    row = _truck_dict(truck)
    transporter = truck.transporter
    row['transporter_id'] = transporter.pk
    row['transporter'] = _truck_transporter_label(transporter)
    assignment = _agent_truck_assignments(request.user, now).get(pk)
    return JsonResponse({
        'ok': True,
        'now': now.isoformat(),
        'truck': row,
        'tracking': assignment if assignment else {'active': False, 'status': 'Idle'},
    })


@login_required
def profile_view(request):
    profile, _created = Profile.objects.get_or_create(user=request.user)
    context = {'profile': profile, 'active_tab': 'profile'}
    return render(request, 'users/profile.html', context)


def _primary_address(user):
    return user.addresses.filter(is_primary=True).first() or user.addresses.first()


def _profile_dict(user, profile):
    addr = _primary_address(user)
    return {
        'ok': True,
        'profile': {
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'bio': profile.bio,
            'phone': profile.phone,
            'theme': profile.theme,
            'notification_pref': profile.notification_pref,
            'street': addr.street if addr else '',
            'city': addr.city if addr else '',
            'country': addr.country if addr else '',
            'picture': profile.profile_picture.url if profile.profile_picture else '',
        },
        'addresses': [_address_dict(a) for a in user.addresses.all()],
        'linked_transporters': [
            {
                'id': t.pk,
                'company_name': t.company_name,
                'alias': t.alias,
                'company_id': t.company_id,
            }
            for t in user.linked_transporters.all()
        ] if user.role == CustomUser.Role.AGENT else [],
    }


def _address_dict(address):
    return {
        'id': address.pk,
        'label': address.label,
        'street': address.street,
        'city': address.city,
        'postal_code': address.postal_code,
        'country': address.country,
        'is_primary': address.is_primary,
    }


@login_required
def api_profile(request):
    profile, _created = Profile.objects.get_or_create(user=request.user)
    return JsonResponse(_profile_dict(request.user, profile))


@login_required
def api_profile_save(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    profile, _created = Profile.objects.get_or_create(user=request.user)
    form = ProfileForm(request.POST, request.FILES, instance=profile)
    if not form.is_valid():
        return JsonResponse({
            'ok': False,
            'error': 'Please fix the highlighted fields.',
            'errors': form.errors,
        })
    email = form.cleaned_data.get('email')
    if email != request.user.email:
        if CustomUser.objects.filter(email=email).exclude(pk=request.user.pk).exists():
            return JsonResponse({'ok': False, 'error': 'That email address is already in use.'})
        request.user.email = email
        request.user.save(update_fields=('email',))
    first_name = (request.POST.get('first_name') or '').strip()[:150]
    last_name = (request.POST.get('last_name') or '').strip()[:150]
    if first_name != request.user.first_name or last_name != request.user.last_name:
        request.user.first_name = first_name
        request.user.last_name = last_name
        request.user.save(update_fields=('first_name', 'last_name'))
    uploaded = request.FILES.get('profile_picture')
    if request.POST.get('remove_picture') in ('1', 'true', 'True') and profile.profile_picture:
        profile.profile_picture.delete(save=False)
    elif uploaded:
        try:
            resized = _resize_profile_picture(uploaded)
            if profile.profile_picture:
                profile.profile_picture.delete(save=False)
            profile.profile_picture.save(resized.name, resized, save=False)
        except Exception:
            return JsonResponse({'ok': False, 'error': 'The uploaded picture could not be processed.'})
    form.save()
    profile.refresh_from_db()
    theme = (request.POST.get('theme') or '').strip()
    if theme in Profile.Theme.values:
        profile.theme = theme
    notification_pref = (request.POST.get('notification_pref') or '').strip()
    if notification_pref in Profile.Notifications.values:
        profile.notification_pref = notification_pref
    profile.save(update_fields=('theme', 'notification_pref'))
    return JsonResponse({
        'ok': True,
        'message': 'Profile saved successfully.',
        'profile': _profile_dict(request.user, profile)['profile'],
    })


@login_required
@require_POST
def api_address_save(request, pk=None):
    instance = None
    if pk is not None:
        instance = Address.objects.filter(user=request.user, pk=pk).first()
        if instance is None:
            return JsonResponse({'ok': False, 'error': 'Address not found.'})
    form = AddressForm(_json_body(request), instance=instance)
    if not form.is_valid():
        return JsonResponse({
            'ok': False,
            'error': 'Please fix the highlighted fields.',
            'errors': form.errors,
        })
    address = form.save(commit=False)
    address.user = request.user
    if address.is_primary:
        Address.objects.filter(user=request.user, is_primary=True).exclude(pk=address.pk).update(is_primary=False)
    address.save()
    return JsonResponse({'ok': True, 'message': 'Address saved successfully.', 'address': _address_dict(address)})


@login_required
@require_POST
def api_address_delete(request, pk):
    address = Address.objects.filter(user=request.user, pk=pk).first()
    if address is None:
        return JsonResponse({'ok': False, 'error': 'Address not found.'})
    label = address.label or address.street or 'Address'
    address.delete()
    return JsonResponse({'ok': True, 'message': f'{label} deleted successfully.'})


@require_POST
def api_login(request):
    if request.user.is_authenticated:
        return JsonResponse({'ok': True, 'redirect': _default_landing_url(request.user)})
    data = _json_body(request)
    identifier = (data.get('identifier') or data.get('email') or '').strip()
    password = data.get('password') or ''
    if not identifier or not password:
        return JsonResponse({'ok': False, 'error': 'Please enter your email/phone and password.'})
    user = authenticate(
        request,
        email=identifier,
        phone=identifier,
        password=password,
    )
    if user is None:
        return JsonResponse({'ok': False, 'error': 'Invalid email/phone or password.'})
    login(request, user)
    return JsonResponse({'ok': True, 'redirect': _default_landing_url(user)})


@require_POST
def api_signup(request):
    if request.user.is_authenticated:
        return JsonResponse({'ok': True, 'redirect': _default_landing_url(request.user)})
    data = _json_body(request)
    form = SignUpForm(data)
    if not form.is_valid():
        return JsonResponse({
            'ok': False,
            'error': 'Please fix the highlighted fields.',
            'errors': form.errors,
        })
    user = form.save()
    login(request, user)
    return JsonResponse({'ok': True, 'redirect': _default_landing_url(user)})