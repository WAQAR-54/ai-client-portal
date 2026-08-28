from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django import forms

from accounts.models import User


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "class": "input"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "input"}),
    )


class SignupForm(UserCreationForm):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autofocus": True, "class": "input"}))

    class Meta:
        model = User
        fields = ["email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["class"] = "input"
        self.fields["password2"].widget.attrs["class"] = "input"


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "input"}),
            "last_name": forms.TextInput(attrs={"class": "input"}),
        }
