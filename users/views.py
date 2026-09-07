import json

from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import AddressForm, ProfileForm, SignUpForm
from .models import Address, CustomUser, Profile


class EmailLoginView(LoginView):
    template_name = 'registration/login.html'
    redirect_authenticated_user = True


email_login = EmailLoginView.as_view()


def signup(request):
    if request.user.is_authenticated:
        return redirect('tenders:dashboard')
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('tenders:dashboard')
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
def profile_view(request):
    profile, _created = Profile.objects.get_or_create(user=request.user)
    context = {'profile': profile, 'active_tab': 'profile'}
    return render(request, 'users/profile.html', context)


def _profile_dict(user, profile):
    return {
        'ok': True,
        'profile': {
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'bio': profile.bio,
            'phone': profile.phone,
            'picture': profile.profile_picture.url if profile.profile_picture else '',
        },
        'addresses': [_address_dict(a) for a in user.addresses.all()],
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
    form.save()
    return JsonResponse({
        'ok': True,
        'message': 'Profile saved successfully.',
        'profile': _profile_dict(request.user, form.instance)['profile'],
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
        return JsonResponse({'ok': True, 'redirect': '/tenders/'})
    data = _json_body(request)
    user = authenticate(
        request,
        email=data.get('email'),
        password=data.get('password'),
    )
    if user is None:
        return JsonResponse({'ok': False, 'error': 'Invalid email or password.'})
    login(request, user)
    return JsonResponse({'ok': True, 'redirect': '/tenders/'})


@require_POST
def api_signup(request):
    if request.user.is_authenticated:
        return JsonResponse({'ok': True, 'redirect': '/tenders/'})
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
    return JsonResponse({'ok': True, 'redirect': '/tenders/'})