from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

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