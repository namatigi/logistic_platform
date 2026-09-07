import json

from django.contrib.auth import authenticate, login
from django.contrib.auth.views import LoginView
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import SignUpForm


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
        return {}


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